# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from datetime import timedelta
from markupsafe import Markup
import requests
import logging
import re
from shapely.geometry import Point, Polygon
_logger = logging.getLogger(__name__)


class WhatsappBotConversation(models.Model):
    """Conversación básica gestionada por el bot"""
    _name = 'whatsapp.bot.conversation'
    _description = 'WhatsApp Bot Conversation'
    _order = 'last_message_date desc, id desc'
    _rec_name = 'partner_id'

    partner_id = fields.Many2one('res.partner', string='Contacto', required=True, index=True)
    mobile_number = fields.Char(string='Número WhatsApp', required=True, index=True)
    mobile_number_formatted = fields.Char(string='Número Formateado', index=True)
    ticket_id = fields.Many2one(
        'helpdesk.ticket',
        string='Ticket asociado',
        ondelete='cascade',
        index=True,
    )
    menu_history = fields.Json(
        string='Historial de Menús',
        default=list,
        help="Lista de IDs de pasos con next_action='next' visitados. Usado para 'Volver al menú anterior'."
    )
    # Estado
    state = fields.Selection([
        ('active', 'Activa'),
        ('timeout', 'Timeout'),
        ('transferred', 'Transferida a Soporte'),
        ('closed', 'Cerrada')
    ], default='active', required=True, index=True)
    
    # Temporalidad
    start_date = fields.Datetime(string='Inicio', default=fields.Datetime.now, required=True)
    last_message_date = fields.Datetime(string='Último Mensaje', default=fields.Datetime.now, required=True)
    timeout_hours = fields.Integer(string='Timeout (horas)', default=24)

    # Campos para gestión de timeouts
    last_user_response_date = fields.Datetime(
        string='Última Respuesta del Usuario',
        default=fields.Datetime.now,
        required=True,
        help="Fecha del último mensaje DEL USUARIO (no del bot)"
    )
    reminder_sent = fields.Boolean(
        string='Aviso Enviado',
        default=False,
        help="Indica si ya se envió el mensaje de aviso por inactividad"
    )
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
    bot_type = fields.Selection(
        related='script_id.bot_type',
        string="Tipo de Bot",
        store=True,
        index=True,
        readonly=True,
    )

    
    # Relaciones
    message_ids = fields.One2many('whatsapp.bot.conversation.message', 'conversation_id', string='Mensajes')
    message_count = fields.Integer(compute='_compute_message_count', store=True)
    wa_account_id = fields.Many2one('whatsapp.account', string='Cuenta WhatsApp', required=True)
    script_id = fields.Many2one('whatsapp.bot.script', string='Script Activo')
    current_step_id = fields.Many2one('whatsapp.bot.script.step', string='Paso Actual')
    conversation_memory = fields.Json(string='Memoria de Conversación', default=dict)
    last_user_input = fields.Text(string='Última Respuesta del Usuario', readonly=True)
    map_token = fields.Char(string="Token maps")
    user_comment = fields.Text(string='Comentario de usuario',readonly=True)

    @api.depends('message_ids')
    def _compute_message_count(self):
        for conv in self:
            conv.message_count = len(conv.message_ids)

    def _reset_conversation(self):
        """Reinicia la conversación"""
        self.write({
            'current_step_id': self.script_id.step_ids[:1].id if self.script_id.step_ids else False,
            'conversation_memory': {},
            'state': 'active',
        })
        _logger.info("🔄 Conversación %s reiniciada", self.id)

    def _should_reset_conversation(self):
        """Verifica si la conversación debe reiniciarse por timeout"""
        self.ensure_one()
        if self.state != 'active':
            return False
        
        timeout_delta = timedelta(hours=self.timeout_hours)
        timeout_limit = self.last_message_date + timeout_delta
        should_reset = fields.Datetime.now() > timeout_limit
        
        if should_reset:
            self.write({'state': 'timeout'})
            _logger.info("⏰ Conversación %s cerrada por timeout (último mensaje: %s)", 
                        self.id, self.last_message_date)
        
        return should_reset

    @api.model
    def _find_or_create_conversation(self, wa_account_id, partner_id, mobile_number, mobile_number_formatted,bot_type='principal'):
        """Busca conversación activa o crea una nueva con script por defecto"""
        self = self.sudo()
        
        from datetime import timedelta
        active_timeout = fields.Datetime.now() - timedelta(hours=24)
        script = self.env['whatsapp.bot.script'].search(
            [
                ('active', '=', True),
                ('bot_type', '=', bot_type),
            ],
            order='sequence',
            limit=1
        )
        conversation = self.search([
            ('partner_id', '=', partner_id),
            ('mobile_number_formatted', '=', mobile_number_formatted),
            ('state', '=', 'active'),
            ('last_message_date', '>=', active_timeout)
        ], limit=1)
        
        if not conversation:
            # Crear nueva conversación con script por defecto
            if not script:
                script = self.env['whatsapp.bot.script'].search([('active', '=', True)], order='sequence', limit=1)
                if not script:
                    raise UserError(_("No hay scripts de bot configurados"))
            
            conversation = self.create({
                'wa_account_id': wa_account_id,
                'partner_id': partner_id,
                'mobile_number': mobile_number,
                'mobile_number_formatted': mobile_number_formatted,
                'script_id': script.id,
                'timeout_hours': script.timeout_hours,
                'current_step_id': script.step_ids[:1].id if script.step_ids else False,
            })
            
            # Enviar primer mensaje si existe
            first_step = script.step_ids[:1]
            if first_step and first_step.step_type in ['message','location_request'] and first_step.message:
                _logger.info("Enviando primer mensaje")
                message = self._render_message_template(first_step.message,partner_id)
                _logger.info(message)
                conversation.send_bot_response(message)
            
            _logger.info("🆕 Nueva conversación bot creada ID %s para %s", conversation.id, partner_id)
        
        return conversation

    def send_bot_response(self, message_text,step=False):
        """
        Envía una respuesta automática a la conversación.
        Crea whatsapp.message para usar la funcionalidad de envío existente
        y guarda el mensaje en whatsapp.bot.conversation.message
        """
        self.ensure_one()

        
        #if not message_text or not message_text.strip():
        #    _logger.warning("Mensaje vacío, no se enviará respuesta")
        #    return False
        components= False
        attachments = False
        location_request = False
        
        if step:
            if step.step_type == 'location_request':
                location_request = True
            if step.button_ids:
                components = step._get_button_components()
                _logger.warning(step)
                _logger.warning(components)
            if step.document_id:
                attachments = step.document_id
                _logger.warning("Adjunto agregado!")
        try:
            # 1. Crear mail.message temporal (necesario para whatsapp.message)
            # Usamos un mail.message sin res_model/res_id ya que no está vinculado a un canal
            mail_message = self.env['mail.message'].create({
                'body': message_text,
                'message_type': 'comment',
                'subtype_id': self.env.ref('mail.mt_comment').id,
                'author_id': self.env.user.partner_id.id,
                'partner_ids': [(4, self.partner_id.id)],
            })

            # 2. Crear whatsapp.message para usar la funcionalidad de envío
            wa_message = self.env['whatsapp.message'].create({
                'wa_account_id': self.wa_account_id.id,
                'mobile_number': self.mobile_number,
                'mobile_number_formatted': self.mobile_number_formatted,
                'body': message_text,
                'mail_message_id': mail_message.id,
                'message_type': 'outbound',
                'state': 'outgoing',
                'bot_conversation_id': self.id,  # Vincular a la conversación del bot
            })

            # 3. Guardar mensaje en nuestra conversación (antes de enviar)
            conversation_message = self.env['whatsapp.bot.conversation.message'].create({
                'conversation_id': self.id,
                'message_type': 'outbound',
                'body': message_text,
                'state': 'pending',
            })

            # 4. Enviar inmediatamente usando el método existente de whatsapp.message
            wa_message._send_message(False,components,attachments,location_request=location_request)
            
            # 5. Actualizar estado del mensaje en nuestra conversación
            conversation_message.write({
                'state': 'sent',
                'message_uid': wa_message.msg_uid if wa_message.msg_uid else False,
            })
            
            # 6. Actualizar último mensaje de la conversación
            self.last_message_date = fields.Datetime.now()
            
            _logger.info("✅ Bot respondió en conversación %s: %s", self.id, message_text[:50])
            
            return True
            
        except Exception as e:
            _logger.error("❌ Error enviando respuesta en conversación %s: %s", self.id, str(e), exc_info=True)
            
            # Marcar mensaje como fallido
            if 'conversation_message' in locals():
                conversation_message.write({
                    'state': 'failed',
                    'error_message': str(e),
                })
            
            return False

    def process_user_message(self, user_input,type):
        """
        Flujo correcto:
        1. Recibir mensaje del usuario
        2. VALIDAR contra el PASO ACTUAL (current_step_id) si tiene validación
        3. Si validación FALLA → enviar fallback del paso actual y NO buscar más
        4. Si validación PASA (o no hay validación) → buscar primer paso posterior que coincida
        5. Si encuentra paso → ejecutarlo y avanzar current_step_id
        6. Si NO encuentra paso → enviar fallback del paso actual (si existe) o mensaje genérico
        """
        self.ensure_one()

        
        if self.state != 'active':
            _logger.info("Conversación %s no está activa, ignorando mensaje", self.id)
            return

        is_location = isinstance(user_input, dict) and user_input.get('type') == 'location'
        if isinstance(user_input, dict) and user_input.get('type') == 'location':
            # Guardar en memoria directamente
            memory = self.conversation_memory or {}
            
            memory.update({
                'direccion_formateada': user_input.get('address'),
                'latitud': user_input.get('lat'),
                'longitud': user_input.get('lng'),
            })
            
            self.conversation_memory = memory
            
            _logger.info("📍 Ubicación guardada en memoria: %s", memory)
            
            # Para que el flujo siga funcionando
            user_input_text = user_input.get('address') or ''
        else:
            user_input_text = user_input.strip()
        # Guardar última respuesta para debugging
        self.write({
            'last_user_response_date': fields.Datetime.now(),
            'reminder_sent': False,  # Permitir nuevo aviso en próxima inactividad
            'last_user_input': user_input_text,
        })
        
        # Reiniciar si hay timeout
        if self._should_reset_conversation():
            self._reset_conversation()
            # Después de reset, procesar el mensaje nuevamente
            self.process_user_message(user_input_text)
            return
        
        current_step = self.current_step_id
        
        # ✅ PASO 1: Validar contra el PASO ACTUAL si tiene validación
        if current_step and current_step.validation_condition_type != 'none':
            validation_input = user_input if is_location else user_input_text
            if not self._validate_response(current_step, validation_input):
                # ❌ Validación falló → enviar fallback y detenerse
                if current_step.validation_fallback_message:
                    self.send_bot_response(current_step.validation_fallback_message)
                    _logger.info("❌ Validación fallida en paso '%s', enviado fallback", current_step.name)
                else:
                    self.send_bot_response("🤖 Respuesta inválida. Por favor intenta nuevamente.")
                    _logger.info("❌ Validación fallida en paso '%s', sin fallback definido", current_step.name)
                if current_step.step_type in ['location_request', 'message']:
                    rendered_message = self._render_message_template(current_step.message)
                    self.send_bot_response(rendered_message, current_step)
                # ❌ NO avanzar, permanecer en el mismo paso esperando respuesta válida
                return
        if current_step and current_step.trigger_memory_key:
            self._save_step_memory(current_step, user_input_text)

        # ✅ PASO 2: Buscar PRIMER paso POSTERIOR que coincida con el mensaje
        next_step = self._find_next_matching_step(user_input_text)
        if next_step:
            # ✅ Guardar en memoria si corresponde
            #self._save_step_memory(next_step, user_input.strip())
            
            # ✅ Ejecutar el paso encontrado
            self._execute_step(next_step, user_input_text)

            if next_step.next_action == 'next' and next_step.step_type in ('message', 'action'):
                history = self.menu_history or []
                # Evitar duplicados consecutivos
                if not history or history[-1] != next_step.id:
                    history.append(next_step.id)
                    self.write({'menu_history': history})
                    _logger.debug("📚 Menú '%s' agregado al historial: %s", next_step.name, history)
            # ✅ Avanzar al paso ejecutado
            _logger.info("Next step")
            _logger.info(next_step)
            if next_step.step_type not in ('back', 'jump', 'back_to_menu'):
                self.write({'current_step_id': next_step.id})
            #self._configure_timeouts(next_step)
            if next_step.next_action == 'auto':
                self._execute_next_auto_step()
            _logger.info("✅ Paso ejecutado: %s (ID %s)", next_step.name, next_step.id)
            
        else:
            # ❌ NINGÚN paso posterior coincide
            if current_step and current_step.validation_fallback_message:
                self.send_bot_response(current_step.validation_fallback_message)
                _logger.info("❌ Sin paso coincidente, enviado fallback del paso actual: %s", current_step.name)
            else:
                self.send_bot_response("🤖 No entendí tu respuesta. Por favor intenta nuevamente.")
                _logger.info("❌ Sin paso coincidente para: %s", user_input[:30])
            # ❌ NO avanzar de paso (permanecer en current_step_id actual)

    def _save_step_memory(self, step, user_input):
        """
        Guarda en memoria:
        1. memory_value (si existe) → valor semántico fijo del paso
        2. user_input (si trigger_memory_key está definido) → respuesta original del usuario
        
        Prioridad: memory_value sobreescribe user_input si usan la misma clave.
        """
        self.ensure_one()
        memory = self.conversation_memory or {}
        
        # Guardar valor semántico fijo del paso (si está definido)
        if step.memory_value and step.trigger_memory_key:
            memory[step.trigger_memory_key] = step.memory_value
            _logger.info("💾 Guardado en memoria [%s]: '%s' (valor semántico del paso)", 
                        step.trigger_memory_key, step.memory_value)
        
        # Guardar respuesta del usuario (si no hay memory_value o clave diferente)
        elif step.trigger_memory_key and user_input.strip():
            memory[step.trigger_memory_key] = user_input.strip()
            _logger.info("💾 Guardado en memoria [%s]: '%s' (respuesta del usuario)", 
                        step.trigger_memory_key, user_input.strip())
        
        # Actualizar memoria de la conversación
        if memory:
            self.conversation_memory = memory

    def _execute_next_auto_step(self):
        """
        Ejecuta automáticamente el siguiente paso sin esperar respuesta del usuario.
        Se usa cuando un paso tiene next_action='auto'.
        """
        self.ensure_one()
        
        # Buscar el siguiente paso que coincida (sin esperar input del usuario)
        next_step = self._find_next_matching_step('')  # Input vacío para pasos con trigger_condition_type='none'
        
        if next_step:
            _logger.info("⚡ Ejecutando paso automático: %s (ID %s)", next_step.name, next_step.id)
            
            # Ejecutar el paso
            self._execute_step(next_step, '')
            
            # Avanzar al paso ejecutado
            self.current_step_id = next_step
            
            # Si este paso también tiene next_action='auto', continuar la cadena
            if next_step.next_action == 'auto':
                self._execute_next_auto_step()
            elif next_step.next_action == 'end' or next_step.step_type == 'end':
                self.write({'state': 'closed'})
        else:
            _logger.info("ℹ️ No hay paso siguiente para ejecución automática")


    def _find_next_matching_step(self, user_input):
        """
        Busca el PRIMER paso POSTERIOR al current_step_id cuya condición de activación coincida.
        Orden: sequence ascendente.
        """
        self.ensure_one()
        all_steps = self.script_id.step_ids.sorted('sequence')
        memory = self.conversation_memory or {}
        _logger.info("Memoria")
        _logger.info(memory)
        # Encontrar posición del current_step_id
        if self.current_step_id:
            try:
                current_index = list(all_steps).index(self.current_step_id)
                search_steps = all_steps[current_index + 1:]  # Solo pasos posteriores
            except ValueError:
                search_steps = all_steps  # current_step_id no está en la lista
        else:
            search_steps = all_steps  # Nueva conversación, buscar desde el inicio
        
        # Buscar PRIMER paso cuya condición coincida
        for step in search_steps:
            # Verificar parent_step_id primero (más rápido)
            if step.parent_step_id and step.parent_step_id != self.current_step_id:
                continue  # Este paso requiere un padre específico que no coincide
            _logger.info(step.required_memory_key)
            _logger.info(step.required_memory_value)
            if step.required_memory_key:
                if step.required_memory_key not in memory:
                    _logger.debug("⏭️ Paso '%s' omitido: falta clave '%s' en memoria", 
                                 step.name, step.required_memory_key)
                    continue
                
                # ✅ Condición 4: Verificar required_memory_value (si está definido)
                if step.required_memory_value:
                    memory_value = str(memory.get(step.required_memory_key, '')).strip()
                    required_value = step.required_memory_value.strip()
                    
                    # Comparación case-insensitive por defecto (mejor UX)
                    if memory_value.lower() != required_value.lower():
                        _logger.debug("⏭️ Paso '%s' omitido: memoria[%s]='%s' ≠ '%s'", 
                                     step.name, step.required_memory_key, memory_value, required_value)
                        continue
            # Verificar condición de activación
            if self._step_condition_matches(step, user_input):
                return step
            
        
        return None  # Ningún paso coincide

    def _step_condition_matches(self, step, user_input):
        """Evalúa si la condición de activación del paso coincide con la entrada del usuario"""
        if step.trigger_condition_type == 'none':
            return True  # Sin condición = siempre coincide (paso inicial)

        if step.trigger_condition_type in ('partner_field_empty', 'partner_field_not_empty'):
            return self._check_partner_field_condition(step)
        
        if not user_input or not step.trigger_condition_value:
            return False
        
        text = user_input.strip()
        pattern = step.trigger_condition_value.strip()
        
        # Case sensitivity
        if not step.trigger_case_sensitive:
            text = text.lower()
            pattern = pattern.lower()
        
        # Evaluar según tipo de condición
        if step.trigger_condition_type == 'contains':
            keywords = [k.strip() for k in pattern.split(',') if k.strip()]
            return any(kw in text for kw in keywords)
        
        elif step.trigger_condition_type == 'equals':
            return text == pattern
        
        elif step.trigger_condition_type == 'starts_with':
            return text.startswith(pattern)
        
        elif step.trigger_condition_type == 'regex':
            try:
                return bool(re.search(pattern, text))
            except re.error:
                _logger.warning("Regex inválido en paso %s: %s", step.name, pattern)
                return False
        
        return False

    def _check_partner_field_condition(self, step):
        """
        Verifica si un campo del partner cumple la condición especificada.
        Útil para pedir datos faltantes (ej: DNI) o mostrar información solo si existe.
        """
        self.ensure_one()
        partner = self.partner_id
        
        if not partner:
            _logger.warning("No hay partner asociado a la conversación %s", self.id)
            return False
        
        field_name = step.trigger_condition_value.strip()
        if not field_name:
            _logger.warning("No se especificó nombre de campo para verificación en paso %s", step.name)
            return False
        
        # Verificar si el campo existe en el modelo partner
        if field_name not in partner._fields:
            _logger.warning("El campo '%s' no existe en res.partner", field_name)
            return False
        
        # Obtener el valor del campo
        field_value = getattr(partner, field_name, False)
        
        # Normalizar valor para verificación (convertir a string si es necesario)
        if isinstance(field_value, models.BaseModel):
            # Para campos relacionales (many2one), considerar vacío si no hay registro
            is_empty = not field_value or not field_value.id
        elif isinstance(field_value, (list, tuple)):
            # Para campos one2many/many2many
            is_empty = not field_value
        else:
            # Para campos simples (char, text, integer, etc.)
            is_empty = not field_value or str(field_value).strip() in ('', 'False', 'None')
        
        # Evaluar según el tipo de condición
        if step.trigger_condition_type == 'partner_field_empty':
            return is_empty  # Coincide si el campo está vacío
        
        elif step.trigger_condition_type == 'partner_field_not_empty':
            return not is_empty  # Coincide si el campo tiene valor
        
        return False

    def _validate_response(self, step, user_input):
        if step.validation_condition_type == 'none':
            return True
    
        if not user_input:
            return False
    
        is_location = isinstance(user_input, dict)
        text = user_input.strip() if not is_location else None
    
        if step.validation_condition_type == 'contains':
            if is_location:
                return False
    
            pattern = step.validation_condition_value.strip()
            options = [opt.strip() for opt in pattern.split(',') if opt.strip()]
    
            if not step.trigger_case_sensitive:
                text = text.lower()
                options = [opt.lower() for opt in options]
    
            return any(opt in text for opt in options)
    
        elif step.validation_condition_type == 'regex':
            if is_location:
                return False
    
            pattern = step.validation_condition_value.strip()
            try:
                return bool(re.fullmatch(pattern, text))
            except re.error:
                return False
    
        elif step.validation_condition_type == 'number':
            if is_location:
                return False
    
            return bool(re.match(r'^-?\d+(\.\d+)?$', text))
    
        elif step.validation_condition_type == 'location':
            return self.validate_location(step, user_input)
    
        elif step.validation_condition_type == 'image':
            if is_location:
                return False
    
            return self.validate_image(step, user_input)
    
        return True

    def validate_image(self, step, user_input):
        """Valida solo por extensión de archivo (versión ligera)"""
        if not user_input or not isinstance(user_input, str):
            return False
        
        VALID_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.pdf', '.doc', '.docx'}
        
        # Limpiar URL y extraer extensión
        clean = user_input.strip().lower().split('?')[0].split('#')[0]
        
        if '.' not in clean:
            return False
        
        _, ext = clean.rsplit('.', 1)
        return f".{ext}" in VALID_EXTENSIONS

    def _execute_step(self, step, user_input):
        """Ejecuta un paso del flujo"""
        self.ensure_one()
        
        # Manejar tipo 'back': Volver al menú anterior

        if step and step.trigger_memory_key:
            self._save_step_memory(step, user_input.strip())
        if step.step_type == 'back':
            self._execute_back_step(step)
            return
        if step.step_type == 'jump':
            if step.target_step_id and step.target_step_id.script_id == step.script_id:
                # Mostrar mensaje del paso destino si lo tiene
                if step.target_step_id.step_type == 'message' and step.target_step_id.message:
                    rendered_message = self._render_message_template(step.target_step_id.message)
                    self.send_bot_response(rendered_message)
                
                # ✅ Persistir el cambio de current_step_id en BD
                self.write({'current_step_id': step.target_step_id.id})
                _logger.info("🔀 Salto a paso específico: %s (ID %s)", 
                            step.target_step_id.name, step.target_step_id.id)
                
                # Si el paso destino tiene next_action='auto', ejecutar en cadena
                if step.target_step_id.next_action == 'auto':
                    self._execute_next_auto_step()
            else:
                _logger.warning("⚠️ Paso 'jump' sin target_step_id válido en paso %s", step.name)
                self.send_bot_response("🤖 Opción no disponible en este momento.")
            return  # ← Importante: return para no continuar con lógica posterior
        # Renderizar y enviar mensaje (para step_type='message')
        # Manejar tipo 'back_to_menu': Volver al último menú con next_action='next'
        if step.step_type == 'back_to_menu':
            history = self.menu_history or []
            if history:  # Al menos 1 menú en el historial
                # ✅ Tomar el último menú del historial (el más reciente) SIN modificar la lista aún
                previous_menu_id = history[-1]
                previous_menu = self.env['whatsapp.bot.script.step'].browse(previous_menu_id)
                
                if previous_menu.exists() and previous_menu.script_id == step.script_id:
                    # Mostrar mensaje del menú
                    if previous_menu.step_type == 'message' and previous_menu.message:
                        rendered_message = self._render_message_template(previous_menu.message)
                        self.send_bot_response(rendered_message)
                    
                    # ✅ Actualizar historial: quitar el último elemento para que el próximo "Volver" vaya más atrás
                    new_history = history[:-1] if len(history) > 1 else []
                    
                    self.write({
                        'current_step_id': previous_menu.id,
                        'menu_history': new_history  # Historial actualizado
                    })
                    _logger.info("🔙 Volviendo al menú: %s (ID %s) | Historial actualizado: %s", 
                                previous_menu.name, previous_menu.id, new_history)
                    return
            
            # Fallback si no hay historial válido
            _logger.warning("⚠️ No hay menú en el historial para volver")
            self.send_bot_response("🤖 No hay menú anterior al que volver.")
            return

        
        if step.step_type in ['message','location_request'] and (step.message or step.document_id):
            rendered_message = self._render_message_template(step.message if step.message else '')
            self.send_bot_response(rendered_message,step)
        
        # ✅ NUEVO: Ejecutar acción predefinida o personalizada (para step_type='action')
        if step.step_type == 'action' and step.action_type != 'none':
            _logger.info("Ejecutando acción!")
            try:
                if step.action_type == 'save_dni':
                    self._action_save_dni(step, user_input)
                elif step.action_type == 'create_ticket':
                    self._action_create_ticket(step, user_input)
                    _logger.info("Crear ticket activado!")
                elif step.action_type == 'register_consulta': 
                    self._action_register_consulta(step, user_input)
                elif step.action_type == 'custom' and step.python_code:
                    self._action_custom_python(step, user_input)
                elif step.action_type == 'save_rating':
                    self._action_save_rating(step, user_input)
                elif step.action_type == 'save_user_comment':
                    self.action_save_user_comment(step, user_input)
                rendered_message = self._render_message_template(step.message)
                self.send_bot_response(rendered_message,step)
                _logger.info("✅ Acción '%s' ejecutada en paso %s", step.action_type, step.name)
                
            except Exception as e:
                _logger.error("❌ Error ejecutando acción '%s' en paso %s: %s", step.action_type, step.name, str(e))
        
        # Finalizar si corresponde
        if step.step_type == 'end' or step.next_action == 'end':
            self.write({'state': 'closed'})

    def action_save_user_comment(self, step, user_input):
        self.ensure_one()
        _logger.warning("Guardando comentario de usuario")
        self.user_comment = user_input.strip() if user_input else ''

    def _normalize_text(self,text):
        if not text:
            return ''
        
        # minúsculas
        text = text.lower()
        
        # quitar emojis / símbolos (deja letras y espacios)
        text = re.sub(r'[^\w\s]', '', text)
        
        return text.strip()
        
    def _action_save_rating(self, step, user_input=None):
        """Guardar calificación en el ticket usando memoria"""
        self.ensure_one()
        _logger.warning("Guardando calificación!!!")
    
        if not self.ticket_id:
            _logger.info("No hay ticket asociado para guardar calificación")
            return
    
        memory = self.conversation_memory or {}
        rating_raw = memory.get('calificacion_bot')
        rating_ticket = memory.get('calificacion_solucion')
        if not rating_raw and not rating_ticket:
            _logger.warning("No hay calificación en memoria")
            return

        text = rating_raw or rating_ticket
        # Normalización
        normalized = self._normalize_text(text)
        if 'excelente' in normalized:
            value = 5
        elif 'normal' in normalized:
            value = 3
        elif 'mala' in normalized:
            value = 1
        else:
            value = None
    
        if not value:
            _logger.warning("Calificación inválida: %s", text)
            return
        if rating_raw:
            self.ticket_id.write({
                'calificacion_bot': value
            })
        elif rating_ticket:
            self.ticket_id.write({
                'calificacion_solucion': value
            })
        _logger.info(
            "⭐ Calificación guardada en ticket %s,%s",
            self.ticket_id.id,
            value
        )

    def _execute_back_step(self, step):
        """
        Ejecuta un paso de tipo 'back': Vuelve al menú anterior (parent_step_id)
        y muestra nuevamente su mensaje.
        """
        self.ensure_one()
        
        # Encontrar el paso padre
        previous_step = step.parent_step_id.parent_step_id
        
        if not previous_step:
            # Si no hay padre, buscar el primer paso del script con trigger_condition_type='none'
            previous_step = self.script_id.step_ids.filtered(
                lambda s: s.trigger_condition_type == 'none'
            )[:1]
            
            if not previous_step:
                _logger.warning("No se encontró paso anterior para volver")
                self.send_bot_response("🤖 No hay menú anterior al que volver.")
                return
        _logger.info("paso anterior")
        _logger.info(previous_step)
        _logger.info(previous_step.message)
        # Mostrar el mensaje del paso padre
        if previous_step.step_type == 'message' and previous_step.message:
            rendered_message = self._render_message_template(previous_step.message)
            _logger.info("Mensaje anterior")
            _logger.info(rendered_message)
            self.current_step_id = previous_step
            _logger.info(self.current_step_id)
            self.send_bot_response(rendered_message)
            # Actualizar current_step_id al paso padre (retroceder en el flujo)
            
            _logger.info("🔙 Volviendo al paso anterior: %s (ID %s)", previous_step.name, previous_step.id)
        else:
            _logger.warning("El paso padre %s no tiene mensaje para mostrar", previous_step.name)
            self.send_bot_response("🤖 No hay menú anterior al que volver.")

    def _action_register_consulta(self, step, user_input):
        """
        Acción predefinida: Registrar consulta sin crear ticket
        Busca dinámicamente todos los layers en memoria (sin límite fijo)
        """
        self.ensure_one()
        partner = self.partner_id
        memory = self.conversation_memory or {}
        
        if not partner:
            _logger.warning("❌ No hay partner para registrar consulta")
            return
        
        # 1) ✅ Buscar TODAS las claves de layer en memoria (dinámico, sin límite)
        layer_codes = []
        layer_keys = []
        
        for key, value in memory.items():
            if value:
                layer_codes.append(value)
                layer_keys.append(key)
        
        if layer_keys:
            sorted_indices = sorted(range(len(layer_keys)), key=lambda k: layer_keys[k])
            layer_codes = [layer_codes[i] for i in sorted_indices]
        
        if not layer_codes:
            _logger.warning("❌ No se encontraron códigos de consulta en memoria")
            return
        
        # 2) Buscar los tipos jerárquicos por código (el search actúa como filtro natural)
        ConsultaTipo = self.env['consulta.tipo']
        tipo_records = self.env['consulta.tipo']
        
        for code in layer_codes:
            tipo = ConsultaTipo.search([('code', '=', code), ('active', '=', True)], limit=1)
            if not tipo:
                tipo = ConsultaTipo.search([('name', 'ilike', code), ('active', '=', True)], limit=1)
            if tipo:
                tipo_records |= tipo
        _logger.info(tipo_records)
        if not tipo_records:
            _logger.warning("⚠️ No se encontró ningún tipo de consulta para códigos: %s", layer_codes)
            return
        
        # 3) Construir ruta completa para subject y descripción
        sorted_types = tipo_records.sorted('sequence')
        full_path = ' / '.join([t.name for t in sorted_types])
        
        # 4) Preparar descripción combinando memoria + input
        lines = []
        # Excluir claves de jerarquía para no duplicar
        exclude_keys = [k for k in memory.keys() if k.startswith('consulta_layer_') or \
                        (k.startswith('layer_') and k.endswith('_code')) or \
                        k.startswith('tipo_consulta_')]
        
        for key, value in memory.items():
            if key not in exclude_keys and value:
                lines.append(f"• {key.capitalize()}: {value}")
        if user_input and user_input.strip():
            lines.append(f"• Comentario: {user_input.strip()}")
        
        description = "\n".join(lines) if lines else (user_input.strip() or "Consulta vía WhatsApp")
        
        # 5) Crear el registro
        ConsultaRegistro = self.env['consulta.registro']
        consulta = ConsultaRegistro.create({
            'partner_id': partner.id,
            'conversation_id': self.id,
            'tipo_ids': [(6, 0, tipo_records.ids)],  # Many2many: asignar todos los tipos
            'subject': f"Consulta: {full_path}",
            'description': description,
            'memory_data': memory.copy(),  # Snapshot para auditoría
        })
        
        _logger.info("✅ Consulta registrada ID %s para %s - Ruta: %s", 
                    consulta.id, partner.name, full_path)
        
        # 6) Confirmar al usuario
        #confirmation_msg = f"✅ Consulta registrada"
        #self.send_bot_response(confirmation_msg)
        
        return consulta

    def _render_message_template(self, template, partner_id=False):
        """Renderiza placeholders en el mensaje"""
        if not template:
            return ''
        
        memory = self.conversation_memory or {}
        if partner_id:
            partner = self.env['res.partner'].browse(partner_id)
        else:
            partner = self.partner_id

        def replace_memory(match):
            key = match.group(1)
            return str(memory.get(key, '')) if key in memory else f'%{{memory.{key}}}'
    
        template = re.sub(r'%\{memory\.([^}]+)\}', replace_memory, template)

        if partner:
            def replace_partner(match):
                field = match.group(1)
                if field == 'open_ticket_count':
                    return str(self._get_partner_open_ticket_count(partner))
                try:
                    value = partner[field]
                    return str(value.name if hasattr(value, 'name') else value) if value else ''
                except Exception:
                    return f'%{{partner.{field}}}'
    
            template = re.sub(r'%\{partner\.([^}]+)\}', replace_partner, template)
        if '%{reclamos}' in template:
            reclamos_txt = self._get_partner_open_reclamos_text(partner)
            template = template.replace('%{reclamos}', reclamos_txt)
    
        return template

    def _get_partner_open_ticket_count(self, partner):
        Ticket = self.env['helpdesk.ticket']
    
        domain = [
            ('partner_id', '=', partner.id),
            ('stage_id.fold', '=', False),
        ]
    
        return Ticket.search_count(domain)

    def _get_partner_open_reclamos_text(self, partner):
        Ticket = self.env['helpdesk.ticket']
    
        domain = [
            ('partner_id', '=', partner.id),
            ('stage_id.fold', '=', False),
        ]
    
        tickets = Ticket.with_context(lang='es_AR').search(domain, order='create_date desc', limit=10)
    
        if not tickets:
            return 'No tenés reclamos abiertos ✅'
    
        lines = ['📌 *Tus reclamos abiertos:*']
    
        for t in tickets:
            fecha = t.create_date.strftime('%d/%m/%Y') if t.create_date else ''
            reclamo_txt = ''
            if t.reclamo_id:
                reclamo_txt = t.reclamo_id.name
                if t.reclamo_subtipo_id:
                    reclamo_txt += f'/{t.reclamo_subtipo_id.name}'
            
            lines.append(f'• #{t.ticket_ref} - {fecha}' + (f' - {reclamo_txt}' if reclamo_txt else '') + f' - {t.stage_id.name}')
        return '\n'.join(lines)

    def _reset_conversation(self):
        """Reinicia la conversación"""
        self.write({
            'current_step_id': False,  # Sin paso enviado aún
            'conversation_memory': {},
            'state': 'active',
        })
        _logger.info("🔄 Conversación %s reiniciada", self.id)


    def _action_save_dni(self, step, user_input):
        """Acción predefinida: Guardar DNI en el partner"""
        self.ensure_one()
        partner = self.partner_id
        memory = self.conversation_memory or {}
        
        # Obtener DNI de la memoria o del input directo
        dni = memory.get(step.trigger_memory_key) if step.trigger_memory_key else user_input.strip()
        
        if dni and partner:
            if len(dni) == 8 and dni.isdigit():
                vat = f"{dni}"
            elif '-' in dni:
                vat = dni  
            else:
                vat = dni
            
            partner.write({'vat': vat})
            _logger.info("✅ DNI/ CUIT %s guardado en partner %s", vat, partner.name)
        else:
            _logger.warning("❌ No se pudo guardar DNI: partner=%s, dni=%s", partner, dni)

    def _action_create_ticket(self, step, user_input):
        """Acción predefinida: Crear ticket de soporte"""
        self.ensure_one()
        partner = self.partner_id
        memory = self.conversation_memory or {}
        
        if not partner:
            _logger.warning("No hay partner para crear ticket")
            return
        
        # Obtener datos del ticket
        lines = []
        for key, value in memory.items():
            if value:
                lines.append(f"{key.capitalize()}: {value}")
        
        ticket_description = "\n".join(lines)
        ticket_subject = f"{self.partner_id.name}-{memory.get('tipo_reclamo','Reclamo Whatsapp')}-{memory.get('subtipo_reclamo','')}"
        only_desc = memory.get('descripcion', '')
        #barrio_id = memory['barrio_id']
        # Crear ticket (ajustar según tu modelo de tickets)
        # Buscar si existe helpdesk.ticket o usar mail.thread como fallback
        ticket_model = self.env['helpdesk.ticket']
        _logger.info(ticket_model)
        vals = {
            'name': ticket_subject[:100],
            'team_id': 1,
            'partner_id': partner.id,
            'description': only_desc,
            'canal_origen': 'whatsapp',
        }
        
        barrio_id = memory.get('barrio_id')
        if barrio_id:
            vals['barrio_id'] = barrio_id
        
        ticket = ticket_model.create(vals)
        self.ticket_id = ticket.id
        _logger.info("Asignar ticket")
        self.assing_ticket_fields(ticket)

        attachments = self.env['ir.attachment'].search([
            ('res_model', '=', 'whatsapp.bot.conversation'),
            ('res_id', '=', self.id)
        ])
        if attachments:
            attachments.write({
                'res_model': 'helpdesk.ticket',
                'res_id': ticket.id,
                'public': True,
            })
            _logger.info("📎 %s adjuntos movidos al ticket %s", len(attachments), ticket.id)
        _logger.info(ticket)
        _logger.info("✅ Ticket creado ID %s para partner %s", ticket.id, partner.name)
        
        # Opcional: enviar confirmación al usuario
        confirmation_msg = f"✅ ¡Recibido! Tu número de reclamo es: {ticket.ticket_ref}."
        self.send_bot_response(confirmation_msg)

    def assing_ticket_fields(self, ticket):
        """
        Asigna automáticamente valores al ticket basándose en la memoria.
        Incluye lógica especial para el nuevo modelo de reclamos.
        """
        self.ensure_one()
        memory = self.conversation_memory or {}
        _logger.info("Asignar!")
        if not memory:
            return
    
        vals = {}
        ticket_fields = ticket._fields
        Reclamo = self.env['reclamo.reclamo']
    
        # -----------------------------
        # 1) Asignar reclamo
        # -----------------------------
        #reclamo = False
        #subtipo_code = memory.get('subtipo_reclamo')
        #tipo_code = memory.get('tipo_reclamo')
        #if subtipo_code:
        #    reclamo = Reclamo.search([('code', '=', subtipo_code)], limit=1)

        #if not reclamo and tipo_code:
        #    reclamo = Reclamo.search([('code', '=', tipo_code)], limit=1)
    
        # fallback por nombre (si no vino código)
        #if not reclamo and subtipo_code:
        #    reclamo = Reclamo.search([('name', 'ilike', subtipo_code)], limit=1)
    
        #if not reclamo and tipo_code:
        #    reclamo = Reclamo.search([('name', 'ilike', tipo_code)], limit=1)
        # -----------------------------
        # 2) Asignación automática estándar
        # -----------------------------
        _logger.info(ticket_fields)
        for key, value in memory.items():
            _logger.info("Key recibido")
            _logger.info(key)
            if key in ticket_fields and value not in (False, None, '', []):
                try:
                    field = ticket_fields[key]
                    if field.type == 'many2one':
                        _logger.info("Procesando many2one %s con valor %s", key, value)
                    
                        rec = False
                    
                        if isinstance(value, int):
                            rec = self.env[field.comodel_name].browse(value)
                        else:
                            comodel = self.env[field.comodel_name]
                    
                            domain = [('name', '=', value)]
                    
                            if 'code' in comodel._fields:
                                domain = ['|', ('code', '=', value), ('name', '=', value)]
                    
                            rec = comodel.search(domain, limit=1)
                    
                        if rec:
                            vals[key] = rec.id
                            _logger.info("Asignado %s -> %s", key, rec.id)
                        else:
                            _logger.warning("No se encontró registro para %s = %s", key, value)
                    elif field.type in ('float', 'integer'):
                        vals[key] = float(value)
    
                    elif field.type == 'boolean':
                        vals[key] = bool(value)
    
                    else:
                        vals[key] = value
    
                except Exception as e:
                    _logger.warning("⚠️ No se pudo asignar campo %s: %s", key, str(e))
    
        if vals:
            ticket.write(vals)
            ticket._apply_reclamo_team()
            parts = []

            if ticket.reclamo_id:
                parts.append(ticket.reclamo_id.name)
        
            if ticket.reclamo_subtipo_id:
                parts.append(ticket.reclamo_subtipo_id.name)
        
            if parts:
                ticket.name = " - ".join(parts)

            _logger.info("🎯 Campos asignados automáticamente al ticket %s: %s", ticket.id, vals)
            
    def _action_custom_python(self, step, user_input):
        """Acción personalizada: Ejecutar código Python"""
        self.ensure_one()
        memory = self.conversation_memory or {}
        partner = self.partner_id
        
        try:
            # Ejecutar el código Python de forma segura
            exec(step.python_code, {
                'record': self,
                'self': self.env,
                'user_input': user_input,
                'memory': memory,
                'partner': partner,
                '_logger': _logger,
                'env': self.env,
                '__builtins__': {
                    'str': str,
                    'int': int,
                    'float': float,
                    'bool': bool,
                    'len': len,
                    're': re,
                    'datetime': __import__('datetime'),
                }
            })
        except Exception as e:
            raise UserError(f"Error en código Python personalizado: {str(e)}")


    def _configure_timeouts(self, step):
        """
        Configura los timeouts de la conversación según el paso ejecutado.
        Se llama DESPUÉS de ejecutar un paso para aplicar sus timeouts.
        """
        self.ensure_one()
        
        # Usar timeouts del paso, con fallback a valores por defecto
        reminder_timeout = step.reminder_timeout_seconds or 0
        close_timeout = step.close_timeout_seconds or 3600  # 1 hora por defecto
        
        self.write({
            'reminder_timeout_seconds': reminder_timeout,
            'close_timeout_seconds': close_timeout,
            'reminder_sent': False,  # Resetear flag de aviso
        })
        
        _logger.info("⏱️ Timeouts configurados para conversación %s: aviso=%ds, cierre=%ds", 
                    self.id, reminder_timeout, close_timeout)

    @api.model
    def _cron_check_inactivity(self):
        """
        CRON: Verifica conversaciones inactivas y envía avisos o cierra según timeouts del SCRIPT.
        """
        _logger.info("⏰ Verificando inactividad en conversaciones...")
    
        now = fields.Datetime.now()
        conversations = self.search([
            ('state', '=', 'active'),
            ('last_user_response_date', '!=', False),
            ('script_id', '!=', False),
        ])
    
        for conv in conversations:
            try:
                script = conv.script_id
                if not script:
                    continue
    
                elapsed = (now - conv.last_user_response_date).total_seconds()
    
                reminder_timeout = script.reminder_timeout_seconds or 0
                close_timeout = script.close_timeout_seconds or 3600
    
                _logger.debug(
                    "Conv %s → elapsed=%ss reminder=%ss close=%ss",
                    conv.id, elapsed, reminder_timeout, close_timeout
                )
    
                # 1️⃣ Enviar aviso
                if (
                    reminder_timeout > 0
                    and elapsed >= reminder_timeout
                    and not conv.reminder_sent
                ):
                    reminder_text = script.reminder_message or "⌛ Sigues allí? Si necesitás algo más, respondé este mensaje 🙂"
                    conv.send_bot_response(reminder_text)
                    conv.reminder_sent = True
                    _logger.info(
                        "🔔 Aviso enviado en conversación %s (%.0fs)",
                        conv.id, elapsed
                    )
    
                # 2️⃣ Cerrar conversación
                elif elapsed >= close_timeout:
                    close_text = script.close_message or "⏱️ Cerramos esta conversación por inactividad. Si necesitás algo más, no dudes en escribirnos nuevamente."
                    conv.send_bot_response(close_text)
                    conv.write({'state': 'closed'})
                    _logger.info(
                        "🔒 Conversación %s cerrada por inactividad (%.0fs)",
                        conv.id, elapsed
                    )

            except Exception as e:
                _logger.error(
                    "❌ Error procesando inactividad en conversación %s: %s",
                    conv.id, str(e), exc_info=True
                )
    
        _logger.info("✅ Cron de inactividad completado")
    

    def validate_location(self, step, user_input):
        """
        Valida dirección con Google Address Validation API
        y guarda dirección normalizada + lat + lng en memoria
        """
        _logger.warning("Validando location!")
        _logger.info(user_input)
        if isinstance(user_input, dict):
            lat = user_input.get('lat')
            lng = user_input.get('lng')
    
            if not self._is_within_municipality(lat, lng):
                return False
            barrio = self.env['geo.barrio']._get_barrio_from_coords(lat, lng)
            memory = self.conversation_memory or {}
            memory.update({
                'latitud': lat,
                'longitud': lng,
                'direccion_formateada': user_input.get('address'),
                'barrio_id': barrio.id if barrio else False,
                'barrio': barrio.name if barrio else 'Sin identificar'
            })
            self.conversation_memory = memory
    
            return True

        if not user_input or len(user_input.strip()) < 5:
            return False
    
        api_key = self.script_id.map_token or self.env['ir.config_parameter'].sudo().get_param('google.maps.api_key')
        if not api_key:
            _logger.error("❌ No hay API KEY de Google configurada")
            return True
    
        url = f"https://addressvalidation.googleapis.com/v1:validateAddress?key={api_key}"
    
        payload = {
            "address": {
                "regionCode": "AR",
                "locality": "Alvear",
                "addressLines": [user_input.strip()]
            }
        }
    
        try:
            response = requests.post(url, json=payload, timeout=5)
            response.raise_for_status()
            data = response.json()
    
            _logger.info("📍 Google response: %s", data)
    
            result = data.get("result", {})
            verdict = result.get("verdict", {})
    
            complete = verdict.get("addressComplete", False)
            action = verdict.get("possibleNextAction")
            
            if not complete or action != "ACCEPT":
                _logger.warning("⚠️ Dirección no validada: %s", verdict)
                return True
    
            address = result.get("address", {})
            geocode = result.get("geocode", {}).get("location", {})
    
            formatted = address.get("formattedAddress")
            lat = geocode.get("latitude")
            lng = geocode.get("longitude")
            
            if not self._is_within_municipality(lat, lng):
                _logger.warning("❌ Dirección fuera del polígono permitido")
                return True
    
            if not all([formatted, lat, lng]):
                _logger.warning("⚠️ Respuesta incompleta de Google: %s", data)
                return True
            barrio = self.env['geo.barrio']._get_barrio_from_coords(lat, lng)
            # Guardar en memoria
            memory = self.conversation_memory or {}
            memory.update({
                'direccion_formateada': formatted,
                'latitud': lat,
                'longitud': lng,
                'barrio_id': barrio.id if barrio else False,
                'barrio': barrio.name if barrio else 'Sin identificar'
            })
            self.conversation_memory = memory
    
            _logger.info("✅ Dirección validada y guardada: %s (%s, %s)", formatted, lat, lng)
            return True
    
        except requests.exceptions.RequestException as e:
            _logger.error("❌ Error llamando Google Maps API: %s", str(e), exc_info=True)
            return True

    def _get_municipality_polygon(self):
        """
        Convierte tu GeoJSON en un Polygon de Shapely
        """
        return Polygon([
            (-60.573270314269394, -33.04017741043106),
            (-60.69258781717268, -33.04404773931585),
            (-60.7083736961868, -33.112958530493124),
            (-60.61636976413756, -33.10233615355508),
            (-60.573270314269394, -33.04017741043106),
        ])

    def _is_within_municipality(self, lat, lon):
        """
        Valida si una coordenada está dentro del área
        """
        if lat is None or lon is None:
            return False
    
        polygon = self._get_municipality_polygon()
    
        point = Point(lon, lat)  # ⚠️ orden correcto
    
        return polygon.contains(point)

class WhatsappBotConversationMessage(models.Model):
    """Mensaje individual dentro de una conversación del bot"""
    _name = 'whatsapp.bot.conversation.message'
    _description = 'WhatsApp Bot Conversation Message'
    _order = 'create_date asc, id asc'

    conversation_id = fields.Many2one('whatsapp.bot.conversation', required=True, ondelete='cascade', index=True)
    message_type = fields.Selection([('inbound', 'Recibido'), ('outbound', 'Enviado')], required=True, index=True)
    
    # Datos del mensaje
    message_uid = fields.Char(string='Message UID WhatsApp', index=True)
    body = fields.Text(string='Contenido', required=True)
    raw_data = fields.Json(string='Datos Raw del Webhook')
    
    # Metadata
    timestamp = fields.Char(string='Timestamp WhatsApp')
    sender = fields.Char(string='Remitente')
    attachment_id = fields.Many2one('ir.attachment', string="Adjunto")
    # Estado (para mensajes salientes)
    state = fields.Selection([
        ('pending', 'Pendiente'),
        ('sent', 'Enviado'),
        ('delivered', 'Entregado'),
        ('read', 'Leído'),
        ('failed', 'Fallido')
    ], default='sent', required=True)  # inbound siempre 'sent', outbound inicia como 'pending'
    
    error_message = fields.Text(string='Error')