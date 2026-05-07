from odoo import models, fields, api, _

class ConsultaRegistro(models.Model):
    """Registro histórico de consultas realizadas por WhatsApp"""
    _name = 'consulta.registro'
    _description = 'Registro de Consulta'
    _order = 'create_date desc, id desc'

    # === Relación con origen ===
    partner_id = fields.Many2one('res.partner', string='Contacto', required=True, index=True)
    conversation_id = fields.Many2one(
        'whatsapp.bot.conversation',
        string='Conversación',
        ondelete='set null',
        index=True
    )
    
    # === Tipos de consulta (todos los niveles jerárquicos) ===
    tipo_ids = fields.Many2many(
        'consulta.tipo',
        'consulta_registro_tipo_rel',  # tabla intermedia explícita
        string='Tipos de Consulta',
        required=True,
        domain="[('active', '=', True)]",
        index=True,
        help="Todos los niveles jerárquicos seleccionados (ej: Nivel 1, Nivel 2, Nivel 3)"
    )
    
    # === Ruta completa computada para display ===
    full_path_display = fields.Char(
        compute='_compute_full_path',
        store=True,
        string='Ruta Completa',
        help="Ej: 'TÉCNICO / GARANTÍA / ELÉCTRICO'"
    )
    full_path_text = fields.Char(
        compute='_compute_full_path',
        store=True,
        index=True,
        string='Ruta para Búsqueda'
    )
    
    # === Contenido de la consulta ===
    subject = fields.Char('Asunto', help="Título breve de la consulta")
    description = fields.Text('Descripción', required=True)
    
    # === Datos adicionales desde memoria del bot ===
    memory_data = fields.Json(
        string='Datos de Memoria',
        default=dict,
        help="Snapshot de la memoria del bot al momento de registrar"
    )
    
    # === Estado y seguimiento ===
    state = fields.Selection([
        ('new', 'Nueva'),
        ('in_progress', 'En Proceso'),
        ('resolved', 'Resuelta'),
        ('archived', 'Archivada'),
    ], default='new', required=True, index=True)
    
    notes = fields.Text('Notas Internas')

    # === Métodos computados ===
    @api.depends('tipo_ids', 'tipo_ids.sequence')
    def _compute_full_path(self):
        """Construye la ruta completa ordenada por secuencia de los tipos"""
        for rec in self:
            if rec.tipo_ids:
                # Ordenar por secuencia para mantener el orden jerárquico correcto
                sorted_types = rec.tipo_ids.sorted('sequence')
                names = [t.name for t in sorted_types]
                codes = [t.code for t in sorted_types]
                rec.full_path_display = ' / '.join(names)
                rec.full_path_text = ' / '.join(codes).lower()
            else:
                rec.full_path_display = ''
                rec.full_path_text = ''

    @api.depends('full_path_display')
    def _compute_subject(self):
        """Genera asunto automático si no se definió"""
        for rec in self:
            if not rec.subject and rec.full_path_display:
                rec.subject = f"Consulta: {rec.full_path_display}"

    # === Búsqueda rápida ===
    @api.model
    def _name_search(self, name, args=None, operator='ilike', limit=100):
        args = args or []
        if name:
            args += [
                '|', '|', '|',
                ('subject', operator, name),
                ('description', operator, name),
                ('full_path_text', operator, name.lower()),
                ('tipo_ids.name', operator, name),
            ]
        return self._search(args, limit=limit)

class ConsultaTipo(models.Model):
    """Tipos y Subtipos de Consultas - Estructura jerárquica"""
    _name = 'consulta.tipo'
    _description = 'Tipos de Consulta'
    _parent_store = True
    _order = 'parent_path, sequence, id'

    name = fields.Char('Nombre', required=True, translate=True)
    code = fields.Char('Código', required=True, index=True, help="Código único para vincular con memoria del bot")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    # Jerarquía
    parent_id = fields.Many2one(
        'consulta.tipo',
        string='Tipo Padre',
        ondelete='cascade',
        index=True
    )
    parent_path = fields.Char(index=True)
    child_ids = fields.One2many('consulta.tipo', 'parent_id', string='Subtipos')

    # Metadata opcional
    description = fields.Text('Descripción', translate=True)
    requires_info = fields.Boolean(
        'Requiere información adicional',
        help="Si está marcado, el bot pedirá detalles extra antes de registrar"
    )

    _sql_constraints = [
        ('code_unique', 'UNIQUE(code)', 'El código de tipo de consulta debe ser único'),
    ]