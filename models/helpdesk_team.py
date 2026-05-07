from odoo import models, fields, api, _

class Secretaria(models.Model):
    _name = 'helpdesk.secretary'
    _description = 'Secretaria'

    name = fields.Char('Nombre', required=True)

    team_ids = fields.One2many('helpdesk.team', 'secretary_id', string='Equipos Asignados')

class HelpdeskTeam(models.Model):
    _inherit = 'helpdesk.team'

    secretary_id = fields.Many2one('helpdesk.secretary', string='Secretaria', ondelete='set null')
