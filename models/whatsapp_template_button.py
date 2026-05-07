# Part of Odoo. See LICENSE file for full copyright and licensing details.

from urllib.parse import urlparse

from odoo import api, fields, models, _
from odoo.addons.phone_validation.tools import phone_validation
from odoo.exceptions import UserError, ValidationError
from odoo.tools.urls import urljoin as url_join


class WhatsappTemplateButton(models.Model):
    _inherit = 'whatsapp.template.button'

    wa_template_id = fields.Many2one(required=False)
    wa_bot_step_id = fields.Many2one(comodel_name='whatsapp.bot.script.step', index=True, ondelete='cascade')
