from odoo import models, fields, api, _

class Reclamo(models.Model):
    _name = 'reclamo.reclamo'
    _description = 'Tipos y Subtipos de Reclamos'
    _parent_store = True
    _order = 'parent_path, sequence, id'

    name = fields.Char('Nombre', required=True, translate=False)
    code = fields.Char('Código', required=True, index=True)
    sequence = fields.Integer(default=10)

    parent_id = fields.Many2one(
        'reclamo.reclamo',
        string='Reclamo Padre',
        ondelete='cascade',
        index=True
    )
    parent_path = fields.Char(index=True)
    child_ids = fields.One2many('reclamo.reclamo', 'parent_id', string='Subtipos')

    team_id = fields.Many2one(
        'helpdesk.team',
        string='Equipo de Soporte',
        help='Equipo al que se deriva este tipo de reclamo'
    )

    user_id = fields.Many2one(
        'res.users',
        string='Derivar a',
        help='Usuario responsable del reclamo'
    )

    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('code_unique', 'unique(code)', 'El código debe ser único.')
    ]