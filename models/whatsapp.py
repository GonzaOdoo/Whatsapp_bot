from odoo import models, fields, api, _
import json
import logging
import re
from datetime import timedelta
from odoo.tools import plaintext2html
from markupsafe import Markup

_logger = logging.getLogger(__name__)


class WhatsappMessage(models.Model):
    _inherit = 'whatsapp.message'

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        
        # Procesar solo mensajes entrantes nuevos
        inbound_messages = messages.filtered(
            lambda m: m.message_type == 'inbound' 
            and m.state == 'received'
            and m.mail_message_id  # Asegurar que tiene mail.message asociado
            and not m.parent_id  # Ignorar respuestas a mensajes salientes
        )
        
        for message in inbound_messages:
            try:
                # Programar procesamiento post-commit para evitar problemas de transacción
                self.env.cr.postcommit.add(
                    lambda mid=message.id: self.env['whatsapp.message']._process_bot_reply_by_id(mid)
                )
                _logger.info("🤖 Programada respuesta bot para mensaje %s", message.id)
            except Exception as e:
                _logger.warning("Error programando respuesta bot para mensaje %s: %s", message.id, str(e))
        
        return messages

    @api.model
    def _process_bot_reply_by_id(self, message_id):
        """Procesamiento seguro post-commit con manejo de estado conversacional"""
        message = self.browse(message_id)
        if not message.exists() or message.state != 'received':
            return
            
        try:
            # 1. Obtener canal asociado al mensaje
            channel = self._get_channel_from_message(message)
            if not channel:
                _logger.warning("No se encontró canal para mensaje %s", message_id)
                return

            # 2. Reiniciar conversación si hay timeout
            if channel._bot_should_reset():
                _logger.info("Reiniciando conversación por timeout en canal %s", channel.id)
                channel._bot_reset_conversation()

            # 3. Actualizar timestamp de interacción
            channel.bot_last_interaction = fields.Datetime.now()
            
            # 4. Extraer texto plano del mensaje
            user_input = self._extract_plain_text(message.body).strip()
            if not user_input:
                return

            # 5. Determinar script a usar (nuevo script si no hay activo o coincide trigger)
            if not channel.bot_script_id:
                script = self._find_matching_script(user_input)
                if script:
                    channel.bot_script_id = script
                    channel.bot_memory = {}  # Reiniciar memoria al cambiar de script
                    _logger.info("Activado script '%s' para canal %s", script.name, channel.id)
            
            # 6. Procesar paso actual
            _logger.info(channel.bot_step_id)
            if channel.bot_step_id:
                self._process_current_step(channel, user_input)
            else:
                # Primer mensaje: activar primer paso del script
                self._start_conversation(channel, user_input)
                
        except Exception as e:
            _logger.error("❌ Error procesando mensaje %s: %s", message_id, str(e), exc_info=True)
            # En caso de error grave, reiniciar conversación para evitar bloqueos
            try:
                channel._bot_reset_conversation()
            except:
                pass

    def _get_channel_from_message(self, message):
        """Obtiene el canal de discusión asociado al mensaje de WhatsApp"""
        if message.mail_message_id and message.mail_message_id.model == 'discuss.channel':
            return self.env['discuss.channel'].browse(message.mail_message_id.res_id)
        
        # Fallback: buscar por partner y número
        if message.partner_id and message.mobile_number_formatted:
            return self.env['discuss.channel'].search([
                ('channel_type', '=', 'whatsapp'),
                ('whatsapp_partner_id', '=', message.partner_id.id),
                ('whatsapp_number', '=', message.mobile_number_formatted)
            ], limit=1)
        return None

    def _find_matching_script(self, message_text):
        """Encuentra el primer script cuyos triggers coincidan con el mensaje"""
        # Scripts ordenados por secuencia, con triggers específicos primero
        scripts = self.env['whatsapp.bot.script'].search([('active', '=', True)], order='sequence, id')
        
        for script in scripts:
            if script._match_trigger(message_text):
                return script
        
        # Si no hay coincidencias, usar primer script activo (script por defecto)
        return scripts[0] if scripts else None

    def _start_conversation(self, channel, user_input):
        """Inicia una nueva conversación con el primer paso del script"""
        if not channel.bot_script_id or not channel.bot_script_id.step_ids:
            _logger.warning("Script sin pasos definidos para canal %s", channel.id)
            return
            
        first_step = channel.bot_script_id.step_ids.sorted('sequence')[0]
        channel.bot_step_id = first_step
        
        # Enviar mensaje del primer paso
        if first_step.message:
            self._send_bot_message(channel, first_step.message, first_step.buttons)

    def _process_current_step(self, channel, user_input):
        """Procesa la respuesta del usuario en el paso actual"""
        current_step = channel.bot_step_id
        memory = channel.bot_memory or {}
        
        # Validar entrada según configuración del paso
        is_valid = self._validate_input(current_step, user_input)
        
        if is_valid:
            # Guardar en memoria si corresponde
            if current_step.memory_key:
                memory[current_step.memory_key] = user_input
                channel.bot_memory = memory
            
            # Ejecutar acción si corresponde
            if current_step.action_server_id:
                try:
                    current_step.action_server_id.with_context(
                        bot_memory=memory,
                        bot_channel_id=channel.id,
                        bot_user_input=user_input
                    ).run()
                except Exception as e:
                    _logger.error("Error ejecutando acción en paso %s: %s", current_step.name, str(e))
            
            # Avanzar al siguiente paso
            next_step = current_step.next_step_id
            if next_step and next_step.step_type != 'end':
                channel.bot_step_id = next_step
                self._send_bot_message(channel, next_step.message, next_step.buttons)
            else:
                # Finalizar conversación
                channel._bot_reset_conversation()
                if next_step and next_step.message:  # Mensaje de despedida
                    self._send_bot_message(channel, next_step.message)
        else:
            # Entrada inválida: usar paso de fallback o reenviar mensaje actual
            fallback_step = current_step.fallback_step_id or current_step
            self._send_bot_message(
                channel, 
                fallback_step.message or "No entendí tu respuesta. Por favor intenta nuevamente.",
                fallback_step.buttons
            )

    def _validate_input(self, step, user_input):
        """Valida la entrada del usuario según la configuración del paso"""
        if step.expected_input == 'any':
            return bool(user_input.strip())
        elif step.expected_input == 'number':
            return user_input.strip().replace('.', '', 1).isdigit()
        elif step.expected_input == 'keywords' and step.valid_keywords:
            keywords = [k.strip().lower() for k in step.valid_keywords.split(',') if k.strip()]
            text = user_input.strip().lower()
            return any(kw in text for kw in keywords)
        return True  # 'text' siempre es válido si no está vacío

    def _send_bot_message(self, channel, message_template, buttons_template=None):
        """Envía un mensaje del bot al canal con renderizado de memoria"""
        if not message_template:
            return
            
        # Renderizar placeholders: %{memory.key} y %{partner.field}
        message_text = self._render_message_template(message_template, channel)
        
        # Obtener cuenta WhatsApp asociada al canal o por defecto
        wa_account = self.env['whatsapp.account'].search([], limit=1)
        if not wa_account:
            _logger.error("No hay cuenta WhatsApp disponible para enviar mensaje")
            return

        try:
            # 1. Crear mail.message en el canal
            mail_message = self.env['mail.message'].create({
                'model': 'discuss.channel',
                'res_id': channel.id,
                'body': Markup('<p>%s</p>') % message_text.replace('\n', '<br/>'),
                'message_type': 'comment',
                'subtype_id': self.env.ref('mail.mt_comment').id,
                'author_id': self.env.user.partner_id.id,
            })

            # 2. Crear whatsapp.message
            wa_message = self.env['whatsapp.message'].create({
                'wa_account_id': wa_account.id,
                'mobile_number': channel.whatsapp_number,
                'mobile_number_formatted': channel.whatsapp_number,
                'body': message_text,
                'mail_message_id': mail_message.id,
                'message_type': 'outbound',
                'state': 'outgoing',
            })

            # 3. Enviar inmediatamente
            wa_message._send()
            _logger.info("✅ Bot respondió en canal %s: %s", channel.id, message_text[:50])
            
        except Exception as e:
            _logger.error("❌ Error enviando mensaje bot: %s", str(e), exc_info=True)

    def _render_message_template(self, template, channel):
        """Renderiza placeholders en el mensaje usando memoria y datos del partner"""
        if not template:
            return ''
        
        memory = channel.bot_memory or {}
        partner = channel.whatsapp_partner_id or channel.partner_id
        
        # Reemplazar %{memory.key}
        def replace_memory(match):
            key = match.group(1)
            return str(memory.get(key, '')) if key in memory else f'%{{{key}}}'
        
        template = re.sub(r'%\{memory\.([^}]+)\}', replace_memory, template)
        
        # Reemplazar %{partner.field}
        if partner:
            def replace_partner(match):
                field = match.group(1)
                try:
                    return str(partner[field]) if field in partner else f'%{{partner.{field}}}'
                except:
                    return f'%{{partner.{field}}}'
            template = re.sub(r'%\{partner\.([^}]+)\}', replace_partner, template)
        
        return template

    @staticmethod
    def _extract_plain_text(html_content):
        """Extrae texto plano de contenido HTML de forma segura"""
        if not html_content:
            return ''
        # Eliminar tags HTML manteniendo saltos de línea significativos
        text = re.sub(r'<br\s*/?>|</p>', '\n', html_content, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text


    def _send_message(self, with_commit=False,buttons=False):
        """ Prepare json data for sending messages, attachments and templates."""
        # init api
        message_to_api = {}
        for account, messages in groupby(self, lambda msg: msg.wa_account_id):
            if not account:
                messages = self.env['whatsapp.message'].concat(*messages)
                messages.write({
                    'failure_type': 'unknown',
                    'failure_reason': 'Missing whatsapp account for message.',
                    'state': 'error',
                })
                self -= messages
                continue
            wa_api = WhatsAppApi(account)
            for message in messages:
                message_to_api[message] = wa_api

        sent_message_vals = set()
        for whatsapp_message in self:
            wa_api = message_to_api[whatsapp_message]
            # try to make changes with current user (notably due to ACLs), but limit
            # to internal users to avoid crash - rewrite me in master please
            if whatsapp_message.create_uid._is_internal():
                whatsapp_message = whatsapp_message.with_user(whatsapp_message.create_uid)
            if whatsapp_message.state != 'outgoing':
                _logger.info("Message state in %s state so it will not sent.", whatsapp_message.state)
                continue
            is_duplicate = False
            msg_uid = False
            try:
                parent_message_id = False
                # body would always come from plaintext2html hence the url text is already the url and references are redundant
                body = html2plaintext(whatsapp_message.body, include_references=False)
                number = whatsapp_message.mobile_number_formatted
                if not number:
                    raise WhatsAppError(failure_type='phone_invalid')
                blacklist_number = wa_phone_validation.wa_phone_format_for_blacklist(number)
                if self.env['phone.blacklist'].sudo().search_count([('number', 'ilike', blacklist_number), ('active', '=', True)], limit=1):
                    raise WhatsAppError(failure_type='blacklisted')

                # based on template
                if template := whatsapp_message.wa_template_id:
                    message_type = 'template'
                    if whatsapp_message.wa_template_id.status != 'approved' or whatsapp_message.wa_template_id.quality == 'red':
                        raise WhatsAppError(failure_type='template')
                    whatsapp_message.message_type = 'outbound'
                    if whatsapp_message.mail_message_id.model != whatsapp_message.wa_template_id.model:
                        raise WhatsAppError(failure_type='template')

                    RecordModel = self.env[whatsapp_message.mail_message_id.model].with_user(whatsapp_message.env.user)
                    from_record = RecordModel.browse(whatsapp_message.mail_message_id.res_id)

                    # if retrying message then we need to unlink previous attachment
                    # in case of header with report in order to generate it again
                    if whatsapp_message.wa_template_id.report_id and whatsapp_message.wa_template_id.header_type == 'document' and whatsapp_message.mail_message_id.attachment_ids:
                        whatsapp_message.mail_message_id.attachment_ids.unlink()

                    # generate sending values, components and attachments
                    send_vals, attachment = whatsapp_message.wa_template_id._get_send_template_vals(
                        record=from_record,
                        whatsapp_message=whatsapp_message,
                    )
                    # reports are considered always unique
                    if not template.report_id:
                        send_vals_without_attachments = dict(send_vals)
                        # the same attachment re-uploaded will have a different identifier
                        # TODO MASTER avoid having to upload as part of the "get template values" flow
                        if template.header_type in ('image', 'video', 'document'):
                            components = [component_vals for component_vals in send_vals['components'] if component_vals['type'] != 'header']
                            send_vals_without_attachments['components'] = components
                        unique_message_vals = (number, frozendict(send_vals_without_attachments))
                        if unique_message_vals not in sent_message_vals:
                            sent_message_vals.add(unique_message_vals)
                        else:
                            is_duplicate = True
                    if attachment and attachment not in whatsapp_message.mail_message_id.attachment_ids:
                        # Clone the attachment to ensure the template's attachment is not affected by message changes
                        cloned_attachment = attachment.copy({'res_model': whatsapp_message.mail_message_id.model, 'res_id': whatsapp_message.mail_message_id.res_id})
                        whatsapp_message.mail_message_id.attachment_ids = [(4, cloned_attachment.id)]
                # no template
                elif whatsapp_message.mail_message_id.attachment_ids:
                    attachment_vals = whatsapp_message._prepare_attachment_vals(whatsapp_message.mail_message_id.attachment_ids[0], wa_account_id=whatsapp_message.wa_account_id)
                    message_type = attachment_vals.get('type')
                    send_vals = attachment_vals.get(message_type)
                    if body:
                        send_vals['caption'] = body
                else:
                    message_type = 'text'
                    send_vals = {
                        'preview_url': True,
                        'body': body,
                    }
                # Tagging parent message id if parent message is available
                if whatsapp_message.mail_message_id and whatsapp_message.mail_message_id.parent_id:
                    parent_id = whatsapp_message.mail_message_id.parent_id.wa_message_ids
                    if parent_id:
                        parent_message_id = parent_id[0].msg_uid
                if not is_duplicate:
                    msg_uid = wa_api._send_whatsapp(number=number, message_type=message_type, send_vals=send_vals, parent_message_id=parent_message_id)
            except WhatsAppError as we:
                whatsapp_message._handle_error(whatsapp_error_code=we.error_code, error_message=we.error_message,
                                               failure_type=we.failure_type)
            except (UserError, ValidationError) as e:
                whatsapp_message._handle_error(failure_type='unknown', error_message=str(e))
            else:
                if is_duplicate:
                    whatsapp_message.state = 'cancel'
                elif whatsapp_message.state == 'outgoing':
                    if not msg_uid:
                        whatsapp_message._handle_error(failure_type='unknown')
                    else:
                        # Message already posted for model 'discuss.channel', post message in active channel for other models
                        if message_type == 'template' and whatsapp_message.wa_template_id.model != 'discuss.channel':
                            whatsapp_message._post_message_in_active_channel()
                        whatsapp_message.write({
                            'state': 'sent',
                            'msg_uid': msg_uid
                        })
                if with_commit:
                    self.env.cr.commit()