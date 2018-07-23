# -*- coding: utf-8 -*-
##############################################################################
#
#    Copyright (C) 2018 Compassion CH (http://www.compassion.ch)
#    Releasing children from poverty in Jesus' name
#    @author: Emanuel Cino <ecino@compassion.ch>
#
#    The licence is in the file __manifest__.py
#
##############################################################################
from datetime import datetime

from dateutil.relativedelta import relativedelta

from odoo import models, api, fields
from odoo.addons.queue_job.job import job, related_action
from odoo.addons.base_phone.fields import Phone
from odoo.addons.child_compassion.models.compassion_hold import HoldType


class SmsChildRequest(models.Model):
    _name = 'sms.child.request'
    _description = 'SMS Child request'
    _rec_name = 'child_id'
    _order = 'date desc'

    sender = Phone(required=True, partner_field='partner_id',
                   country_field='country_id')
    date = fields.Datetime(required=True, default=fields.Datetime.now())
    website_url = fields.Char(readonly=True)
    full_url = fields.Char(compute='_compute_full_url')
    state = fields.Selection([
        ('new', 'Request received'),
        ('child_reserved', 'Child reserved'),
        ('step1', 'Step 1 completed'),
        ('step2', 'Step 2 completed'),
        ('expired', 'Request expired')
    ], default='new')
    partner_id = fields.Many2one('res.partner', 'Partner')
    country_id = fields.Many2one(
        'res.country', related='partner_id.country_id', readonly=True)
    child_id = fields.Many2one(
        'compassion.child', 'Child', ondelete='set null')
    hold_id = fields.Many2one('compassion.hold', related='child_id.hold_id')
    event_id = fields.Many2one(
        'crm.event.compassion', 'Event',
        domain=[('accepts_sms_booking', '=', True)],
        compute='_compute_event', inverse='_inverse_event', store=True
    )
    sponsorship_id = fields.Many2one('recurring.contract', 'Sponsorship')

    # Filter criterias made by sender
    gender = fields.Selection([
        ('Male', 'Male'),
        ('Female', 'Female')
    ])
    min_age = fields.Integer(size=2)
    max_age = fields.Integer(size=2)
    field_office_ids = fields.Many2one(
        'compassion.field.office', 'Field Office')

    @api.multi
    def _compute_full_url(self):
        base_url = self.env['ir.config_parameter'].get_param(
            'web.external.url')
        for request in self:
            request.full_url = base_url + request.website_url

    @api.multi
    @api.depends('date')
    def _compute_event(self):
        for request in self.filtered('date'):
            request.event_id = self.env['crm.event.compassion'].search([
                ('accepts_sms_booking', '=', True),
                ('start_date', '<=', request.date)
            ], limit=1)

    def _inverse_event(self):
        # Allows to manually set an event
        return True

    @api.model
    def create(self, vals):
        if 'partner_id' not in vals:
            # Try to find a matching partner given phone number
            phone = vals.get('sender')
            partner_obj = self.env['res.partner']
            partner = partner_obj.search([
                '|', ('mobile', 'like', phone),
                ('phone', 'like', phone)
            ])
            if partner and len(partner) == 1:
                vals['partner_id'] = partner.id
        request = super(SmsChildRequest, self).create(vals)
        request.website_url = '/sponsor-now/' + str(request.id)
        # Directly commit for the job to work
        self.env.cr.commit()    # pylint: disable=invalid-commit
        request.with_delay().reserve_child()
        return request

    @api.onchange('partner_id')
    def onchange_partner_id(self):
        phone = self.partner_id.mobile or self.partner_id.phone
        if phone:
            self.sender = phone

    @api.multi
    def change_child(self):
        """ Release current child and take another."""
        self.hold_id.sms_request_id = False
        self.write({
            'state': 'new',
            'child_id': False
        })
        return self.reserve_child()

    @api.multi
    def cancel_request(self):
        self.hold_id.sms_request_id = False
        return self.write({
            'state': 'expired',
            'child_id': False
        })

    @job(default_channel='root.sms_request')
    @related_action(action='related_action_sms_request')
    def reserve_child(self):
        """Finds a child for SMS sponsorship service.
        Put the child on hold for the sms request. If the hold request is
        not working, fallback to search a child already allocated for the
        event.
        """
        self.ensure_one()
        childpool_search = self.env['compassion.childpool.search'].create({
            'take': 1,
            'gender': self.gender,
            'min_age': self.min_age,
            'max_age': self.max_age,
            'field_office_ids': [(6, 0, self.field_office_ids.ids or [])]
        })
        if self.gender or self.min_age or self.max_age or \
                self.field_office_ids:
            childpool_search.do_search()
        else:
            childpool_search.rich_mix()
        expiration = datetime.now() + relativedelta(minutes=15)
        result_action = self.env['child.hold.wizard'].with_context(
            active_id=childpool_search.id, async_mode=False).create({
                'type': HoldType.E_COMMERCE_HOLD.value,
                'expiration_date': fields.Datetime.to_string(expiration),
                'primary_owner': self.env.uid,
                'event_id': self.event_id.id,
                'campaign_id': self.event_id.campaign_id.id,
                'ambassador': self.event_id.user_id.partner_id.id or
                self.env.uid,
                'channel': 'sms',
                'source_code': 'sms_sponsorship',
                'return_action': 'view_holds'
            }).send()
        child_hold = self.env['compassion.hold'].browse(
            result_action['domain'][0][2])
        child_hold.sms_request_id = self.id
        if child_hold.state == 'active':
            self.write({
                'child_id': child_hold.child_id.id,
                'state': 'child_reserved'
            })
        else:
            self._take_child_from_event()
        return True

    def _take_child_from_event(self):
        """ Called in case we couldn't make a hold from global childpool.
        We will search if we have some children available for the sms service.
        """
        available_holds = self.event_id.hold_ids.filtered(
            lambda h: h.channel == 'sms' and not h.sms_request_id)
        if available_holds:
            available_holds[0].sms_request_id = self.id
            self.write({
                'child_id': available_holds[0].child_id.id,
                'state': 'child_reserved'
            })