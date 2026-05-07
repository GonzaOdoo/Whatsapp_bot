from odoo import fields, models


class HelpdeskTicketReportAnalysis(models.Model):
    _inherit = 'helpdesk.ticket.report.analysis'

    reclamo_id = fields.Many2one(
        'reclamo.reclamo',
        string='Tipo de Reclamo'
    )
    reclamo_subtipo_id = fields.Many2one(
        'reclamo.reclamo',
        string='Subtipo de Reclamo')

    def _select(self):
        return super()._select() + """,
            T.reclamo_id AS reclamo_id,
            T.reclamo_subtipo_id AS reclamo_subtipo_id
        """
    
    def _group_by(self):
        return super()._group_by() + """,
            T.reclamo_id,
            T.reclamo_subtipo_id
        """