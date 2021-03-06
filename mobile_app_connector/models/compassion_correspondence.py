# -*- coding: utf-8 -*-
##############################################################################
#
#    Copyright (C) 2018 Compassion CH (http://www.compassion.ch)
#    @author: Quentin Gigon <gigon.quentin@gmail.com>
#
#    The licence is in the file __manifest__.py
#
##############################################################################

from odoo import models, api, fields
from odoo.tools import plaintext2html
from ..mappings.compassion_correspondence_mapping import \
    MobileCorrespondenceMapping, FromLetterMapping
from werkzeug.exceptions import NotFound
from base64 import b64encode

from datetime import date, timedelta


class CompassionCorrespondence(models.Model):
    _inherit = 'correspondence'

    @api.multi
    def get_app_json(self, multi=False):
        """
        Called by HUB when data is needed for a tile
        :param multi: used to change the wrapper if needed
        :return: dictionary with JSON data of the children
        """
        child = self.sudo().mapped('child_id')
        if not self:
            return {}
        mapping = FromLetterMapping(self.env)
        wrapper = 'Letters' if multi else 'Letter'
        if len(self) == 1:
            data = mapping.get_connect_data(self)
        else:
            data = []
            for letter in self:
                data.append(mapping.get_connect_data(letter))
        unread_recently = not(self.email_read and self.email_read <
                              fields.Date.to_string(
                                  date.today() + timedelta(days=3)))
        return {
            'Child': child.get_app_json_no_wrap(),
            wrapper: data,
            'OrderDate': self.sent_date or self.status_date,
            'UnReadRecently': unread_recently,
            }

    @api.model
    def mobile_post_letter(self, json_data, **parameters):
        """
            Mobile app method:
            POST a letter between a child and a sponsor

            :param parameters: all request parameters
            :return: sample response
        """
        # Validate required parameters
        self._validate_required_fields([
            'TemplateID',
            'Message',
            'Need',
            'supporterId',
            'base64string'
            ], json_data)
        mapping = MobileCorrespondenceMapping(self.env)
        vals = mapping.get_vals_from_connect(json_data)
        letter = self.env['correspondence'].create(vals)

        if letter:
            return "Letter Submitted"
        else:
            return "Letter could not be created and was not submitted"

    @api.model
    def mobile_get_letters(self, **other_params):
        """
        Mobile app method:
        Returns all the letters from a Child
        :param needid: beneficiary id
        :param supgrpid: sponsor id
        """
        partner_id = self._get_required_param('supgrpid', other_params)
        child_id = self._get_required_param('needid', other_params)
        letters = self.search([
            ('partner_id', '=', int(partner_id)),
            ('child_id', '=', int(child_id)),
            ('direction', '=', 'Beneficiary To Supporter')
        ])

        mapper = FromLetterMapping(self.env)
        return [mapper.get_connect_data(letter) for letter in letters]

    @api.model
    def mobile_letter_pdf(self, **other_params):
        host = self.env['ir.config_parameter'].get_param('web.external.url')
        letter_id = other_params.get('correspondenceid')
        if letter_id:
            letter = self.browse(int(letter_id))
            if letter.exists() and letter.letter_image:
                letter.email_read = fields.Datetime.now()
                return host + "/b2s_image?id=" + letter.uuid
        raise NotFound("Letter with id {} not found".format(letter_id))

    def _validate_required_fields(self, fields, params):
        missing = [key for key in fields if key not in params]
        if missing:
            raise ValueError(
                'Required parameters {}'.format(','.join(missing)))

    def _get_required_param(self, key, params):
        if key not in params:
            raise ValueError('Required parameter {}'.format(key))
        return params[key]

    def mobile_get_preview(self, *args, **other_params):
        """
        This method is called by the app to retrieve a PDF preview of a letter.
        We get in the params the image and text and build a PDF from there via
        the PDF generator. The image is appended at the end of the HTML text.
        Both text and photo are enclosed in a <div> that will be handled in the
        s2b_letter.xml where the JS handle the multipage layout.
        The app has two write function
            - card: where no template is set and the template id is 0.
            - letter: where we get a valid template id
        :param _:  not used by the controller
        :param other_params: A dictionary containing at least:
                             - 'letter-copy': the HTML text
                             - 'selected_child': the child ID
                             - 'selected-letter-id': the template ID, 0 if we
                                                     take the default one.
                             - 'file_upl': the image
                             - 'path_info_extension': the image extension
        :return: An URL pointing to the PDF preview of the generated letter
        """
        body = self._get_required_param('letter-copy', other_params)
        body_html = '<div id="first_box">' + plaintext2html(body)
        # supporter_id = self._get_required_param('sup-id', other_params)
        child_id = self._get_required_param('selected-child', other_params)
        template_id = \
            self._get_required_param('selected-letter-id', other_params)
        if template_id == '0':
            # write a card -> default template
            template_id = '2'
        file = other_params.get('file_upl')
        file_extension = other_params.get('path_info_extension')
        gen = self.env['correspondence.s2b.generator'].sudo().create({
            'name': 'app',
            'selection_domain': "[('child_id', '=', " + child_id + ")]",
            'body_html': body_html,
            's2b_template_id': template_id,
        })
        if file:
            body_html += "<br><div id='included-img' style='width: 100%;'>" \
                         "<img style='width:100%;"
            body_html += "' src=\"data:image/" + file_extension[:] + \
                         ";base64, " + b64encode(file.stream.read()) + \
                         "\"/></div>"
            gen.body_html = body_html
        gen.body_html += "</div>"
        gen.onchange_domain()
        self.env.cr.commit()
        gen.preview()
        web_base_url = \
            self.env['ir.config_parameter'].get_param('web.external.url')
        url = web_base_url + "/web/image/" + gen._name + "/" + \
            str(gen.id) + "/preview_pdf"
        return url

    @api.multi
    def mobile_send_letter(self, *params, **parameters):
        """
        Function called by the app upon clicking on send letter.
        We search for the s2b_generator that we used for the preview generation
        Once we have it, we proceed to sending it.
        This function is called two times by the app for unknown dark reasons.
        The second time the parameters contains the DbId of the letter we just
        sent. If the parameter is present, we already sent the letter and
        therefore return early.
        :param params: A dictionary containing:
                           - 'TemplateID': ID of the template
                           - 'Need': ID of the child the letter is sent to
                           - 'DbID': (optional) Indicator that the letter was
                                     already sent.
        :return: The ID of the sent letter, never used by the app.
        """
        params = params[0]
        template_id = self._get_required_param('TemplateID', params)
        if 'DbId' in params:
            # The letter was submitted on the last api call
            if template_id == "0":
                return "Letter Submitted "
            return "Card Submitted "
        # body = self._get_required_param('Message', params)
        # partner_id = self._get_required_param('SupporterId', params)
        # iOS and Android do not return the same format
        if template_id == '0' or template_id == 0:
            # write a card -> default template
            template_id = '2'
        child_id = self._get_required_param('Need', params)
        if isinstance(child_id, list):
            child_id = child_id[0]
        child = \
            self.env['compassion.child'].browse(int(child_id))
        gen = self.env['correspondence.s2b.generator'].sudo().search([
            ('name', '=', 'app'),
            ('selection_domain', '=',
             "[('child_id', '=', '" + child.local_id + "')]"),
            ('s2b_template_id', '=', int(template_id)),
            ('state', '=', 'preview')
        ], limit=1, order='create_date DESC')
        gen.generate_letters_job()
        return {
            'DbId': gen.letter_ids.mapped('id'),
        }
