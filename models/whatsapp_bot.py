# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
import re
import logging

_logger = logging.getLogger(__name__)


class WhatsappBotScript(models.Model):
    """Script configurable para el chatbot"""
    _name = 'whatsapp.bot.script'
    _description = 'WhatsApp Bot Script'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    step_ids = fields.One2many('whatsapp.bot.script.step', 'script_id', string='Pasos', copy=True)
    trigger_keywords = fields.Char(
        help="Palabras clave para activar este script (ej: 'soporte, ayuda'). Vacío = script por defecto"
    )
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    timeout_hours = fields.Integer(default=24, string='Timeout (horas)')
        # Campos para gestión de timeouts
    close_timeout_seconds = fields.Integer(
        string='Timeout de Cierre (segundos)',
        default=3600,
        help="Tiempo total de inactividad antes de cerrar"
    )
    reminder_timeout_seconds = fields.Integer(
        string='Timeout de Aviso (segundos)',
        default=0,
        help="Tiempo para enviar aviso si no hay respuesta"
    )
    reminder_message = fields.Text(
        string='Mensaje de Aviso por Inactividad',
        help="Mensaje que se envía cuando el usuario no responde luego del tiempo configurado."
    )
    close_message = fields.Text(
        string='Mensaje de Cierre por Inactividad',
        help="Mensaje que se envía cuando la conversación se cierra por inactividad."
    )
    bot_type = fields.Selection([
        ('principal', 'Principal'),
        ('survey', 'Encuesta'),
        ('resolution', 'Resolución'),
    ], string="Tipo de Bot", required=True, default='principal', index=True)
    map_token = fields.Char(string="API KEY MAPS")
    def _match_trigger(self, message_text):
        """Verifica si el mensaje coincide con los triggers del script"""
        if not self.trigger_keywords:
            return True  # Script por defecto
        keywords = [k.strip().lower() for k in self.trigger_keywords.split(',') if k.strip()]
        text = message_text.strip().lower()
        return any(kw in text for kw in keywords)


class WhatsappBotScriptStep(models.Model):
    """Paso del flujo: se ejecuta SI su condición coincide con el mensaje del usuario"""
    _name = 'whatsapp.bot.script.step'
    _description = 'WhatsApp Bot Script Step'
    _order = 'script_id, sequence, id'

    script_id = fields.Many2one('whatsapp.bot.script', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, translate=True)
    
    # Tipo de paso
    step_type = fields.Selection([
        ('message', 'Enviar Mensaje'),
        ('action', 'Ejecutar Acción'),
        ('auto', 'Paso Automático'),
        ('back', 'Volver al Menú Anterior'),
        ('back_to_menu', 'Volver al Último Menú Visitado'),
        ('jump', 'Saltar a Paso Específico'),
        ('location_request', 'Solicitar Ubicación'),
        ('end', 'Finalizar Conversación')
    ], required=True, default='message')
    
    # Mensaje a enviar CUANDO este paso se active
    message = fields.Text(translate=False, help="Usa %{memory.key} o %{partner.name}")
    
    # Evalúa contra el ÚLTIMO mensaje del usuario
    trigger_condition_type = fields.Selection([
        ('none', 'Siempre ejecutar (paso inicial)'),
        ('contains', 'Contiene texto'),
        ('equals', 'Igual a'),
        ('starts_with', 'Comienza con'),
        ('regex', 'Expresión regular'),
        ('partner_field_empty', 'Campo del contacto vacío'),
        ('partner_field_not_empty', 'Campo del contacto con valor'),
    ], string='Condición para activar este paso', default='none', required=True)
    
    trigger_condition_value = fields.Char(
        string='Valor de condición',
        help="Ej: '1,pedido' para contains, '^PED-\\d+$' para regex"
    )
    trigger_memory_key = fields.Char(
        string='Guardar en memoria',
        help="Clave para guardar la respuesta del usuario que activó este paso (ej: 'pedido_id')"
    )
    trigger_case_sensitive = fields.Boolean(string='Case Sensitive', default=False)
    memory_value = fields.Char(
        string='Valor para Memoria',
        help="Valor fijo que se guardará en la memoria de la conversación cuando este paso se active. "
             "Útil para convertir respuestas genéricas ('1','2') en valores significativos ('cambio_luz','columnas'). "
             "Usa junto con 'Guardar en memoria' para definir la clave."
    )
    # ⚠️ VALIDACIÓN (opcional): Verifica que la respuesta sea válida ANTES de avanzar
    # Si falla, se envía fallback_message y NO se avanza de paso
    validation_condition_type = fields.Selection([
        ('none', 'Sin validación'),
        ('contains', 'Contiene texto'),
        ('regex', 'Expresión regular'),
        ('number', 'Es número'),
        ('location','Geolocalización'),
        ('image','Es una imagen'),
    ], string='Validar respuesta', default='none')
    
    validation_condition_value = fields.Char(string='Patrón de validación')
    validation_fallback_message = fields.Text(
        translate=False,
        string='Mensaje si validación falla',
        help="Se envía si la respuesta NO pasa la validación. El paso NO avanza."
    )
    action_type = fields.Selection([
        ('none', 'Sin acción'),
        ('save_dni', 'Guardar DNI en contacto'),
        ('create_ticket', 'Crear ticket de soporte'),
        ('register_consulta', 'Registrar consulta'),
        ('save_image', 'Guardar imagen'),
        ('save_rating', 'Guardar Calificación'),
        ('save_user_comment', 'Guardar Comentario'),
        ('custom', 'Código Python personalizado'),
    ], string='Tipo de Acción', default='none', 
    help="Acción a ejecutar cuando este paso se active")
    
    python_code = fields.Text(
        string='Código Python Personalizado',
        help="Código Python a ejecutar. Variables disponibles: record, partner, memory, user_input"
    )
    
    # Acción después de ejecutar
    next_action = fields.Selection([
        ('next', 'Esperar siguiente respuesta'),
        ('auto', 'Continuar automáticamente al siguiente paso'),
        ('end', 'Finalizar conversación'),
    ], default='next', string='Después de ejecutar')
    
    # Metadata
    description = fields.Text(translate=True, string='Descripción')
    parent_step_id = fields.Many2one(
        'whatsapp.bot.script.step',
        string='Proviene de',
        domain="[('script_id', '=', script_id), ('id', '!=', id)]",
        help="Este paso solo se activará si el paso anterior fue este. Dejar vacío para activar desde cualquier paso anterior."
    )
    target_step_id = fields.Many2one(
        'whatsapp.bot.script.step',
        string='Saltar a paso',
        domain="[('script_id', '=', script_id), ('id', '!=', id)]",
        help="Paso específico al que se redirigirá la conversación cuando se ejecute este paso. "
             "Solo visible si el tipo de paso es 'jump'."
    )
    required_memory_key = fields.Char(
        string='Requiere clave en memoria',
        help="Este paso SOLO se activará si esta clave existe en la memoria de la conversación. "
             "Útil para mostrar encuestas solo después de ciertas opciones. Ej: 'motivo_reclamo'"
    )
    required_memory_value = fields.Char(
        string='Valor requerido en memoria',
        help="Opcional: además de existir la clave, debe tener este valor exacto. "
             "Ej: 'cambio_luz' para activar solo si motivo_reclamo='cambio_luz'"
    )
    button_ids = fields.One2many('whatsapp.template.button','wa_bot_step_id',string='Boton de whatsapp')
    document_id = fields.Many2one('ir.attachment',string='Documento')
    option_ids = fields.One2many(
        'whatsapp.bot.script.step.option',
        'step_id',
        string='Opciones de Mapeo'
    )

    def _get_button_components(self):
        buttons = []
    
        for button in self.button_ids.sorted('sequence'):
            if button.button_type == 'quick_reply':
                buttons.append({
                    "type": "reply",
                    "reply": {
                        "id": str(button.id),
                        "title": button.name[:20]
                    }
                })
    
            elif button.button_type == 'url':
                buttons.append({
                    "type": "url",
                    "parameters": [{
                        "display_text": button.name,
                        "url": button.website_url
                    }]
                })
    
            elif button.button_type == 'phone_number':
                buttons.append({
                    "type": "phone_number",
                    "parameters": [{
                        "display_text": button.name,
                        "phone_number": button.call_number
                    }]
                })
    
        return buttons


class WhatsappBotStepOption(models.Model):
    _name = 'whatsapp.bot.script.step.option'
    _description = 'Opciones de mapeo del paso'

    step_id = fields.Many2one(
        'whatsapp.bot.script.step',
        required=True,
        ondelete='cascade'
    )

    user_value = fields.Char(
        string="Respuesta del usuario",
        required=True,
        help="Ej: 1"
    )

    mapped_value = fields.Char(
        string="Valor a guardar",
        required=True,
        help="Ej: reclamo"
    )

    description = fields.Char(
        string="Descripción",
        help="Texto opcional para referencia"
    )