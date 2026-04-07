import os, io, json, uuid, logging, sys
from datetime import datetime, timedelta, timezone
import asyncio
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# 🔥 Zona horaria fija de Perú (UTC-5)
ZONA_PERU = timezone(timedelta(hours=-5))

# ======== ENV Y CREDENCIALES ========
load_dotenv(override=True)
BOT_TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID") 
GOOGLE_IMAGES_FOLDER_ID = os.getenv("GOOGLE_IMAGES_FOLDER_ID")
GCP_SA_JSON = os.getenv("GCP_SA_PATH", "credenciales.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

if not os.path.exists(GCP_SA_JSON) and not GCP_SA_JSON.startswith("{"):
    logger.error(f"❌ ERROR CRÍTICO: No se encontró el archivo {GCP_SA_JSON}.")
    sys.exit(1)

if GCP_SA_JSON.endswith(".json"):
    with open(GCP_SA_JSON, 'r', encoding='utf-8') as f:
        service_account_info = json.load(f)
else:
    service_account_info = json.loads(GCP_SA_JSON)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)

# 🔥 TUS ENCABEZADOS AHORA CON 39 COLUMNAS (ESQUELETO ACTUALIZADO)
ENCABEZADOS = [
    "USER_ID", "FECHA_HORA", "TIPO_OPERACION", "MARCA", "TICKET", "COD_CLIENTE", "CLIENTE", "DNI",
    "DIRECCION", "DISTRITO", "CONTRATA", "TECNICO", "CTO",
    "PUERTO_UTILIZADO", "SN_ANTIGUO", "SN_NUEVO", "PHONOWIN", "PROD_ID", 
    "MOTIVO_REMAT", "OBSERVACION", 
    "FOTO_1", "FOTO_2", "FOTO_3", "FOTO_4", "FOTO_5", 
    "MENSAJE_RECHAZO", "SUBSANACION", "POTENCIA ONT Y OLT", "GESTOR", "ESTADO", "NOTIFICADO", 
    "FECHA_HORA FIN", "CTO ANTIGUA", "OLT ANTIGUA", "OLT NUEVO", 
    "ACTUALIZACIÓN TRASLADO", "ACTUALIZACIÓN CAMBIO DE ONT", "ORDENAMIENTO", "TI"
]


# ======== ESTADOS ========
SELECCIONAR_OP, SELECCIONAR_MARCA, PREGUNTAR_DATO, CONFIRMAR_DATO = range(4)
RECIBIR_SUBSANACION = 10 # 🔥 Nuevo estado para el flujo de corrección

# =======================================================
# 🛡️ CHEQUEO ESTRICTO (SOPORTA UNIDADES COMPARTIDAS)
# =======================================================
def validar_entorno_estricto():
    logger.info("🔍 INICIANDO CHEQUEO ESTRICTO DEL ENTORNO...")

    if not SPREADSHEET_ID or not GOOGLE_IMAGES_FOLDER_ID:
        logger.error("❌ ERROR: Faltan los IDs en el archivo .env")
        sys.exit(1)

    correo_bot = service_account_info.get('client_email')
    
    try:
        service = build("drive", "v3", credentials=creds)
        service.files().get(
            fileId=GOOGLE_IMAGES_FOLDER_ID, 
            fields="id", 
            supportsAllDrives=True
        ).execute()
        logger.info("✅ 1/2: Acceso a Carpeta de Google Drive CONFIRMADO.")
    except Exception as e:
        logger.error("❌ ERROR 1/2: No se puede acceder a la carpeta de Drive.")
        sys.exit(1)

    try:
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
        
        primera_fila = sheet.row_values(1)
        if not primera_fila:
            logger.info("📝 El Excel está vacío. Escribiendo encabezados automáticamente...")
            sheet.append_row(ENCABEZADOS)
        
        logger.info("✅ 2/2: Acceso a Google Sheets CONFIRMADO.")
    except Exception as e:
        logger.error("❌ ERROR 2/2: No se puede acceder al Google Sheets.")
        sys.exit(1)

    logger.info("🚀 TODO ESTÁ LISTO Y PERFECTO. EL BOT PUEDE ARRANCAR.")


# ======== TAREA EN SEGUNDO PLANO: VIGILAR CAMBIOS DE ESTADO ========
async def verificar_cambios_estado(context: ContextTypes.DEFAULT_TYPE):
    """Revisa el Excel cada 10s buscando la orden manual 'ENVIAR' del gestor"""
    try:
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
        records = sheet.get_all_records()

        for i, row in enumerate(records, start=2):
            estado = str(row.get("ESTADO", "")).strip().upper()
            notificado = str(row.get("NOTIFICADO", "")).strip().upper()
            user_id = row.get("USER_ID")
            gestor = str(row.get("GESTOR", "-"))
            motivo = str(row.get("MENSAJE_RECHAZO", "-"))
            phonowin = str(row.get("PHONOWIN", "")).strip().upper()
            potencia_final = str(row.get("POTENCIA ONT Y OLT", "-")).strip()
            
            # 🔥 NUEVO: Extraemos el Cliente y el DNI
            cliente = str(row.get("CLIENTE", "N/A"))
            dni = str(row.get("DNI", "N/A"))

            if notificado == "ENVIAR" and user_id:
                ticket = row.get("TICKET", "N/A")
                op = row.get("TIPO_OPERACION", "Operación")
                
                # 🔥 LÓGICA DE AUDITORÍA: Detecta si es Rechazo o Aprobación
                if "RECHAZADO" in estado:
                    mensaje = (
                        f"🚨 *¡ATENCIÓN! TICKET RECHAZADO* 🚨\n\n"
                        f"🎫 *Ticket:* `{ticket}`\n"
                        f"👤 *Cliente:* {cliente}\n"
                        f"🪪 *DNI/CE:* {dni}\n"
                        f"🛠️ *Operación:* {op}\n"
                        f"👨‍💻 *Revisado por:* {gestor}\n"
                        f"❌ *Motivo:* {motivo}\n"
                    )
                    kb = []
                    if "SUBSANAR" in estado:
                        mensaje += "\n⚠️ *Acción Requerida:* Por favor, subsana la evidencia enviando la foto o dato correcto."
                        kb = [[InlineKeyboardButton("🛠️ Subsanar Observación", callback_data=f"SUBSANAR_{ticket}")]]
                    else:
                        mensaje += "\n⚠️ *Nota:* Rechazo por temas externos. No requiere corrección por este medio."

                    try:
                        await context.bot.send_message(chat_id=user_id, text=mensaje, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb) if kb else None)
                        sheet.update_cell(i, ENCABEZADOS.index("NOTIFICADO") + 1, "SI")
                        logger.info(f"✅ Alerta de rechazo enviada a {user_id} para el ticket {ticket}")
                    except Exception as e:
                        logger.error(f"❌ No se pudo enviar mensaje al usuario {user_id}: {e}")

                else:
                    # 🔥 LÓGICA DE TIEMPOS ESTIMADOS SEGÚN PHONOWIN
                    tiempo_activacion = ""
                    if phonowin in ["SÍ", "SI"]:
                        tiempo_activacion = "⏳ *Tiempo estimado de activación:* 15 minutos."
                    elif phonowin == "NO":
                        tiempo_activacion = "⏳ *Tiempo estimado de activación:* 10 minutos."
                    
                    # 🔥 MENSAJE 1: CUANDO PASA A "FINALIZADO"
                    if estado == "FINALIZADO":
                        ahora_cierre = datetime.now(ZONA_PERU).strftime("%Y-%m-%d %H:%M:%S")
                        mensaje = (
                            f"✅ *ACTUALIZACIÓN DE ACTIVACIÓN*\n\n"
                            f"🎫 *Ticket:* `{ticket}`\n"
                            f"👤 *Cliente:* {cliente}\n"
                            f"🪪 *DNI/CE:* {dni}\n"
                            f"🛠️ *Operación:* {op}\n"
                            f"👨‍💻 *Revisado por:* {gestor}\n"
                            f"📍 *Estado Actual:* `{estado}`\n"
                            f"⚡ *Potencias:* `{potencia_final}`\n\n"
                            f"🔥 *HE FINALIZADO TU CASO, EL SERVICIO YA SE ENCUENTRA ACTIVADO.* 🔥\n\n"
                            f"💪 Gracias, recuerda escribir /start para un siguiente registro🚀💪"
                        )
                        # 🔥 SELLA LA FECHA EN LA COLUMNA DEL EXCEL
                        try:
                            sheet.update_cell(i, ENCABEZADOS.index("FECHA FINALIZADO") + 1, ahora_cierre)
                        except Exception as e:
                            logger.error(f"❌ Error sellando fecha final: {e}")
                    
                    # 🔥 MENSAJE 2: CUANDO PASA A "EN REVISIÓN"
                    elif estado == "EN REVISIÓN":
                        mensaje = (
                            f"✅ *ACTUALIZACIÓN DE ACTIVACIÓN*\n\n"
                            f"🎫 *Ticket:* `{ticket}`\n"
                            f"👤 *Cliente:* {cliente}\n"
                            f"🪪 *DNI/CE:* {dni}\n"
                            f"🛠️ *Operación:* {op}\n"
                            f"👨‍💻 *Revisado por:* {gestor}\n"
                            f"📍 *Estado Actual:* `{estado}`\n\n"
                            f"El gestor ha comenzado a procesar tu solicitud."
                        )
                        if tiempo_activacion:
                            mensaje += f"\n\n{tiempo_activacion}"
                            
                    # 🔥 MENSAJE 3: CUALQUIER OTRO ESTADO
                    else:
                        mensaje = (
                            f"✅ *ACTUALIZACIÓN DE ACTIVACIÓN*\n\n"
                            f"🎫 *Ticket:* `{ticket}`\n"
                            f"👤 *Cliente:* {cliente}\n"
                            f"🪪 *DNI/CE:* {dni}\n"
                            f"🛠️ *Operación:* {op}\n"
                            f"👨‍💻 *Revisado por:* {gestor}\n"
                            f"📍 *Estado Actual:* `{estado}`\n\n"
                            f"El gestor ha revisado y actualizado tu solicitud."
                        )

                    try:
                        await context.bot.send_message(chat_id=user_id, text=mensaje, parse_mode="Markdown")
                        sheet.update_cell(i, ENCABEZADOS.index("NOTIFICADO") + 1, "SI")
                        logger.info(f"✅ Notificación de estado enviada a {user_id} para el ticket {ticket}")
                    except Exception as e:
                        logger.error(f"❌ No se pudo enviar mensaje al usuario {user_id}: {e}")

    except Exception as e:
        logger.error(f"❌ Error en la tarea de vigilancia: {e}")
        

# ======== FUNCIONES DE DRIVE Y SHEETS ========
def upload_image_to_google_drive(file_bytes: bytes, filename: str):
    try:
        service = build("drive", "v3", credentials=creds)
        file_metadata = {"name": filename, "parents": [GOOGLE_IMAGES_FOLDER_ID], "mimeType": "image/jpeg"}
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="image/jpeg", resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields="id, webViewLink", supportsAllDrives=True).execute()
        return file["webViewLink"]
    except Exception as e:
        logger.error(f"❌ Error en Drive: {e}")
        return None

def gs_append_row(fila):
    try:
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
        sheet.append_row(fila, value_input_option="USER_ENTERED")
        return True, "✅ Fila inyectada correctamente"
    except Exception as e:
        return False, f"❌ Error general Sheets: {e}"

# ======== DICCIONARIOS DE FLUJOS PASO A PASO ========

# 🔥 ACTUALIZADO: Retiramos PROD_ID de aquí para pedirlo aparte
CAMPOS_PLANTILLA = {
    "CAMBIO_ONT": ["TICKET", "CODIGO CLIENTE", "CLIENTE", "DNI ó CE", "PARTNER", "CUADRILLA", "DISTRITO", "OBSERVACIÓN"],
    "CAMBIO_CTO": ["TICKET", "CODIGO CLIENTE", "CLIENTE", "DNI ó CE", "DIRECCION", "CTO", "PUERTO UTILIZADO", "PARTNER", "CUADRILLA", "DISTRITO", "OBSERVACIÓN"],
    "REMATRICULACION": ["TICKET", "CODIGO CLIENTE", "CLIENTE", "DNI ó CE", "PARTNER", "CUADRILLA", "DISTRITO", "MOTIVO DE REMATRICULACIÓN"],
    "TRASLADO_CAMBIO": ["TICKET", "CODIGO CLIENTE", "CLIENTE", "DNI ó CE", "DIRECCION", "CTO", "PUERTO UTILIZADO", "PARTNER", "CUADRILLA", "DISTRITO", "OBSERVACIÓN"]
}

# 🔥 ACTUALIZADO: Reintegramos PROD_ID como paso independiente donde corresponde
FLUJOS = {
    "CAMBIO_ONT": ["PLANTILLA", "SN_ANTIGUO", "SN_NUEVO", "PROD_ID", "PHONOWIN", "FOTO_ONT_NUEVA", "FOTO_ONT_ANTIGUA", "FOTO_POTENCIA"],
    "CAMBIO_CTO": ["PLANTILLA", "SN_ACTUAL", "PHONOWIN", "FOTO_CTO", "FOTO_PUERTO", "FOTO_ONT_ACTUAL", "FOTO_POTENCIA"],
    "REMATRICULACION": ["PLANTILLA", "SN_ACTUAL", "PROD_ID", "PHONOWIN", "FOTO_ONT_ACTUAL", "FOTO_POTENCIA"],
    "TRASLADO_CAMBIO": ["PLANTILLA", "SN_ANTIGUO", "SN_NUEVO", "PROD_ID", "PHONOWIN", "FOTO_ONT_NUEVA", "FOTO_ONT_ANTIGUA", "FOTO_POTENCIA", "FOTO_CTO", "FOTO_PUERTO"]
}

NOMBRES_OPERACIONES = {
    "CAMBIO_ONT": "1️⃣ CAMBIO DE ONT",
    "CAMBIO_CTO": "2️⃣ TRASLADOS / CAMBIO DE CTO / CAMBIO DE PUERTO",
    "REMATRICULACION": "3️⃣ REMATRICULACIÓN",
    "TRASLADO_CAMBIO": "4️⃣ TRASLADOS / CAMBIO DE CTO / CAMBIO DE PUERTO + CAMBIO DE ONT"
}

PREGUNTAS = {
    "TICKET": "🎫 Ingresa el número de *TICKET*:",
    "COD_CLIENTE": "🆔 Ingresa el *CÓDIGO DEL CLIENTE*:",
    "CLIENTE": "👤 Ingresa el *NOMBRE DEL CLIENTE*:",
    "DNI": "🪪 Ingresa el *DNI ó CE* del cliente:",
    "DIRECCION": "📍 Ingresa la *DIRECCIÓN* del cliente:",
    "CTO": "🏷️ Ingresa el código de la *CTO*:",
    "PUERTO": "🔌 Ingresa el *PUERTO UTILIZADO* de la CTO:",
    "SN_ANTIGUO": "🛜 Ingresa el *SN de la ONT ANTIGUA*:",
    "SN_ACTUAL": "🛜 Ingresa el *SN de la ONT ACTUAL*:",
    "SN_NUEVO": "🛜 Ingresa el *SN de la ONT NUEVA*:",
    "PHONOWIN": "📞 ¿Cuenta con *PHONOWIN*? (Selecciona una opción):",
    "PROD_ID": "🔢 Ingresa el *PRODUCT ID*:",
    "CONTRATA": "🏢 Ingresa el nombre del *PARTNER*:",
    "TECNICO": "👷 Ingresa el nombre y nomenclatura de la *CUADRILLA*:",
    "DISTRITO": "🗺️ Ingresa el *DISTRITO*:",
    "OBS": "📝 Ingresa la *OBSERVACIÓN*:",
    "MOTIVO": "❓ Ingresa el *MOTIVO DE REMATRICULACIÓN*:",
    "FOTO_ONT_NUEVA": "📸 Envía la *FOTO DE LA ONT NUEVA*:",
    "FOTO_ONT_ANTIGUA": "📸 Envía la *FOTO DE LA ONT ANTIGUA*:",
    "FOTO_ONT_ACTUAL": "📸 Envía la *FOTO DE LA ONT ACTUAL*:",
    "FOTO_POTENCIA": "📸 Envía la *FOTO DE LA POTENCIA INTERNA*:",
    "FOTO_CTO": "📸 Envía la *FOTO DE LA CTO CERRADA*:",
    "FOTO_PUERTO": "📸 Envía la *FOTO DEL PUERTO UTILIZADO*:"
}

# =======================================================
# 🛠️ FUNCIONES DE SUBSANACIÓN
# =======================================================
async def iniciar_subsanacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ticket = query.data.replace("SUBSANAR_", "")
    context.user_data["ticket_subsanar"] = ticket
    
    await query.edit_message_text(
        f"🛠️ *SUBSANANDO TICKET: {ticket}*\n\n"
        f"Por favor, envía la nueva evidencia solicitada (Sube la foto corregida o escribe el dato correcto):", 
        parse_mode="Markdown"
    )
    return RECIBIR_SUBSANACION

async def guardar_subsanacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ticket = context.user_data.get("ticket_subsanar")
    msg_subiendo = await update.message.reply_text("☁️ Procesando y enviando corrección al gestor...")

    if update.message.photo or update.message.document:
        file = await (update.message.photo[-1].get_file() if update.message.photo else update.message.document.get_file())
        file_bytes = await file.download_as_bytearray()
        filename = f"SUBSANACION_{ticket}_{datetime.now(ZONA_PERU).strftime('%H%M%S')}.jpg"
        evidencia = upload_image_to_google_drive(file_bytes, filename)
        if not evidencia:
            await update.message.reply_text("❌ Error subiendo la foto a Drive. Intenta de nuevo.")
            return RECIBIR_SUBSANACION
    else:
        evidencia = update.message.text.strip().upper()

    try:
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
        records = sheet.get_all_records()
        row_idx = None
        
        # Busca de abajo hacia arriba para encontrar el más reciente
        for i, row in reversed(list(enumerate(records, start=2))):
            if str(row.get("TICKET", "")) == str(ticket):
                row_idx = i
                break

        if row_idx:
            # Actualiza Subsanación, reinicia el estado y borra el notificado
            sheet.update_cell(row_idx, ENCABEZADOS.index("SUBSANACION") + 1, evidencia)
            sheet.update_cell(row_idx, ENCABEZADOS.index("ESTADO") + 1, "PENDIENTE REVISIÓN")
            sheet.update_cell(row_idx, ENCABEZADOS.index("NOTIFICADO") + 1, "NO")
            
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_subiendo.message_id)
            await update.message.reply_text("✅ *¡Corrección enviada exitosamente!*\n\nEl ticket ha vuelto a la cola del gestor para su revisión final.", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Error: No se encontró el número de ticket en la base de datos.")

    except Exception as e:
        logger.error(f"Error en subsanación: {e}")
        await update.message.reply_text("❌ Ocurrió un error conectando con el sistema.")

    context.user_data.pop("ticket_subsanar", None)
    return ConversationHandler.END

# ======== INICIO ========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["registro"] = {}
    
    mensaje = (
        "👋 *Bienvenido al Registro de Activaciones de WIN 🟠*\n\n"
        "¿Qué operación registramos hoy?\n\n"
        "1⃣ *CAMBIO DE ONT*\n"
        "2⃣ *TRASLADOS / CAMBIO CTO / CAMBIO PUERTO*\n"
        "3⃣ *REMATRICULACIÓN*\n"
        "4⃣ *TRASLADOS / CAMBIO CTO / PUERTO + CAMBIO ONT*"
    )
    
    teclado = [
        [
            InlineKeyboardButton("1⃣", callback_data="OP_CAMBIO_ONT"), 
            InlineKeyboardButton("2⃣", callback_data="OP_CAMBIO_CTO")
        ],
        [
            InlineKeyboardButton("3⃣", callback_data="OP_REMATRICULACION"), 
            InlineKeyboardButton("4⃣", callback_data="OP_TRASLADO_CAMBIO")
        ]
    ]
    
    await update.message.reply_text(mensaje, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="Markdown")
    return SELECCIONAR_OP

async def boton_operacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    op = query.data.replace("OP_", "")
    
    context.user_data["registro"] = {
        "OPERACION": op, "ID_REGISTRO": str(uuid.uuid4())[:6],
        "FLUJO_ACTUAL": FLUJOS[op], "PASO_IDX": 0, "DATOS": {}
    }
    
    teclado_marcas = [
        [InlineKeyboardButton("HUAWEI", callback_data="MARCA_HUAWEI"), InlineKeyboardButton("ZTE", callback_data="MARCA_ZTE")], 
        [InlineKeyboardButton("TP-LINK", callback_data="MARCA_TPLINK")]
    ]
    
    nombre_completo = NOMBRES_OPERACIONES.get(op, op)
    
    await query.edit_message_text(
        f"✅ Operación: *{nombre_completo}*\n\nSelecciona la *MARCA*:", 
        parse_mode="Markdown", 
        reply_markup=InlineKeyboardMarkup(teclado_marcas)
    )
    return SELECCIONAR_MARCA

async def boton_marca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    marca = query.data.replace("MARCA_", "").replace("TPLINK", "TP-LINK")
    context.user_data["registro"]["DATOS"]["MARCA"] = marca
    
    await query.delete_message()
    await hacer_pregunta(query.message.chat_id, context)
    return PREGUNTAR_DATO

# ======== MOTOR PASO A PASO ========
async def hacer_pregunta(chat_id, context):
    reg = context.user_data["registro"]
    idx = reg["PASO_IDX"]
    
    if idx >= len(reg["FLUJO_ACTUAL"]):
        await mostrar_resumen(chat_id, context)
        return
        
    paso_actual = reg["FLUJO_ACTUAL"][idx]
    
    # 🔥 NUEVA LÓGICA: Enviar la plantilla al técnico
    if paso_actual == "PLANTILLA":
        op = reg["OPERACION"]
        campos = CAMPOS_PLANTILLA[op]
        plantilla_str = "\n".join([f"{c}: " for c in campos])
        
        mensaje = (
            "📝 *¡Ahorremos tiempo! Copia el siguiente recuadro, llénalo con los datos del cliente y envíamelo de vuelta en un solo mensaje:*\n\n"
            f"```\n{plantilla_str}\n```\n\n"
            "⚠️ *Nota:* Escribe el dato justo después de los dos puntos (:) y por favor NO alteres los títulos."
        )
        await context.bot.send_message(chat_id, mensaje, parse_mode="Markdown")
        
    elif paso_actual == "PHONOWIN":
        kb = [
            [InlineKeyboardButton("✅ SÍ", callback_data="PHONO_SI"), InlineKeyboardButton("❌ NO", callback_data="PHONO_NO")]
        ]
        await context.bot.send_message(chat_id, PREGUNTAS.get(paso_actual), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await context.bot.send_message(chat_id, PREGUNTAS.get(paso_actual), parse_mode="Markdown")

async def recibir_phonowin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    reg = context.user_data["registro"]
    paso_actual = reg["FLUJO_ACTUAL"][reg["PASO_IDX"]]
    
    respuesta = "SÍ" if query.data == "PHONO_SI" else "NO"
    reg["DATOS"][paso_actual] = respuesta
    
    kb = [
        [InlineKeyboardButton("✅ Confirmar", callback_data="CONFIRMAR"), InlineKeyboardButton("✏️ Corregir", callback_data="CORREGIR")]
    ]
    await query.edit_message_text(f"📝 Registrado correctamente: `{respuesta}`\n\nElige una opción:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    return CONFIRMAR_DATO

async def recibir_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reg = context.user_data["registro"]
    paso_actual = reg["FLUJO_ACTUAL"][reg["PASO_IDX"]]
    
    if "FOTO" in paso_actual:
        await update.message.reply_text("⚠️ Necesito una *imagen*, no texto. Por favor sube la foto.", parse_mode="Markdown")
        return PREGUNTAR_DATO
        
    texto_recibido = update.message.text.strip().upper()
    marca = reg["DATOS"].get("MARCA", "")
    reglas = {"HUAWEI": {"sn": 16, "prod_id": 20}, "ZTE": {"sn": 12, "prod_id": 15}, "TP-LINK": {"sn": 16, "prod_id": 17}}
    
    # 🔥 LÓGICA DE ESCANEO DE PLANTILLA BLINDADA
    if paso_actual == "PLANTILLA":
        lineas = texto_recibido.split('\n')
        op = reg["OPERACION"]
        campos_esperados = CAMPOS_PLANTILLA[op]
        
        # Mini-función ninja para limpiar tildes y espacios y hacer una comparación perfecta
        def norm(t):
            return t.replace("Ó","O").replace("Á","A").replace("É","E").replace("Í","I").replace("Ú","U").replace(" ", "").strip()

        datos_extraidos = {}
        for linea in lineas:
            if ":" in linea:
                clave, valor = linea.split(":", 1)
                clave_norm = norm(clave.upper())
                
                # Buscamos coincidencias con nuestros campos (ambos limpios de tildes/espacios)
                for campo in campos_esperados:
                    if norm(campo.upper()) == clave_norm:
                        datos_extraidos[campo] = valor.strip()
                        break

        # Verificamos si dejaron algo vacío
        faltantes = [c for c in campos_esperados if c not in datos_extraidos or datos_extraidos[c] == ""]
        if faltantes:
            faltantes_str = ", ".join(faltantes)
            await update.message.reply_text(f"❌ *Error:* Faltan llenar estos campos:\n`{faltantes_str}`\n\nPor favor, revisa que hayas llenado todos los datos y envía la plantilla completa de nuevo.", parse_mode="Markdown")
            return PREGUNTAR_DATO

        # Guardamos todo el bloque de datos en la memoria del bot
        for k, v in datos_extraidos.items():
            reg["DATOS"][k] = v
            
        # Le mostramos un mini-resumen de lo que capturó
        resumen = "📝 *Datos capturados correctamente:*\n"
        for k, v in datos_extraidos.items():
            resumen += f"🔸 *{k}:* `{v}`\n"
        resumen += "\n¿La información es correcta?"
        
        kb = [[InlineKeyboardButton("✅ Confirmar", callback_data="CONFIRMAR"), InlineKeyboardButton("✏️ Corregir", callback_data="CORREGIR")]]
        await update.message.reply_text(resumen, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return CONFIRMAR_DATO

    # ======== LÓGICA PARA LOS SERIALES (SN) Y PROD_ID ========
    if marca in reglas:
        if paso_actual in ["SN_NUEVO", "SN_ACTUAL", "SN_ANTIGUO"] and len(texto_recibido) != reglas[marca]["sn"]:
            await update.message.reply_text(f"❌ El SN de {marca} debe tener *{reglas[marca]['sn']} caracteres* (Tiene {len(texto_recibido)}).\nIntenta de nuevo:", parse_mode="Markdown")
            return PREGUNTAR_DATO
        elif paso_actual == "PROD_ID" and len(texto_recibido) != reglas[marca]["prod_id"]:
            await update.message.reply_text(f"❌ El PRODUCT ID de {marca} debe tener *{reglas[marca]['prod_id']} caracteres* (Tiene {len(texto_recibido)}).\nIntenta de nuevo:", parse_mode="Markdown")
            return PREGUNTAR_DATO

    reg["DATOS"][paso_actual] = texto_recibido
    kb = [
        [InlineKeyboardButton("✅ Confirmar", callback_data="CONFIRMAR"), InlineKeyboardButton("✏️ Corregir", callback_data="CORREGIR")]
    ]
    await update.message.reply_text(f"📝 Registrado correctamente: `{texto_recibido}`\n\nElige una opción:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    return CONFIRMAR_DATO

async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reg = context.user_data["registro"]
    paso_actual = reg["FLUJO_ACTUAL"][reg["PASO_IDX"]]
    
    if "FOTO" not in paso_actual:
        await update.message.reply_text("⚠️ Ahora necesito texto, no una foto.")
        return PREGUNTAR_DATO
        
    msg_subiendo = await update.message.reply_text("☁️ Subiendo foto...")
    file = await (update.message.photo[-1].get_file() if update.message.photo else update.message.document.get_file())
    file_bytes = await file.download_as_bytearray()
    
    filename = f"{reg['OPERACION']}_{reg['ID_REGISTRO']}_{paso_actual}.jpg"
    link = upload_image_to_google_drive(file_bytes, filename)
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_subiendo.message_id)
    
    if link:
        reg["DATOS"][paso_actual] = link
        kb = [
            [InlineKeyboardButton("✅ Confirmar", callback_data="CONFIRMAR"), InlineKeyboardButton("📸 Corregir", callback_data="CORREGIR")]
        ]
        await update.message.reply_text("✅ Foto subida correctamente..\n\nElige una opción:", reply_markup=InlineKeyboardMarkup(kb))
        return CONFIRMAR_DATO
    else:
        await update.message.reply_text("❌ Error subiendo a Drive. Intenta de nuevo.")
        return PREGUNTAR_DATO

async def manejar_confirmacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    reg = context.user_data["registro"]
    paso_actual = reg["FLUJO_ACTUAL"][reg["PASO_IDX"]]
    
    if query.data == "CONFIRMAR":
        if paso_actual == "PLANTILLA":
            await query.edit_message_text("✅ *Datos de la plantilla guardados exitosamente.*", parse_mode="Markdown")
        elif "FOTO" in paso_actual:
            await query.edit_message_text(f"✅ Evidencia de *{paso_actual.replace('_', ' ')}* guardada exitosamente.", parse_mode="Markdown")
        else:
            dato = reg["DATOS"][paso_actual]
            await query.edit_message_text(f"✅ *{paso_actual.replace('_', ' ')}* registrado: `{dato}`", parse_mode="Markdown")

        context.user_data["registro"]["PASO_IDX"] += 1
        await hacer_pregunta(query.message.chat_id, context)
        
        if context.user_data["registro"]["PASO_IDX"] >= len(reg["FLUJO_ACTUAL"]):
            return ConversationHandler.END
        return PREGUNTAR_DATO
        
    elif query.data == "CORREGIR":
        if paso_actual == "PLANTILLA":
            op = reg["OPERACION"]
            campos = CAMPOS_PLANTILLA[op]
            plantilla_str = "\n".join([f"{c}: " for c in campos])
            mensaje = (
                "🔄 Entendido. Por favor **copia, corrige y envía de nuevo la plantilla**:\n\n"
                f"```\n{plantilla_str}\n```"
            )
            await query.edit_message_text(mensaje, parse_mode="Markdown")
        elif "FOTO" in paso_actual:
            await query.edit_message_text("🔄 Entendido. Por favor **sube la foto nuevamente**:", parse_mode="Markdown")
        elif paso_actual == "PHONOWIN":
            kb = [
                [InlineKeyboardButton("✅ SÍ", callback_data="PHONO_SI"), InlineKeyboardButton("❌ NO", callback_data="PHONO_NO")]
            ]
            await query.edit_message_text(PREGUNTAS.get(paso_actual), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        else:
            await query.edit_message_text("🔄 Entendido. Por favor **escribe el dato correcto**:", parse_mode="Markdown")
        return PREGUNTAR_DATO

# ======== RESUMEN Y GUARDADO ========
async def mostrar_resumen(chat_id, context):
    reg = context.user_data["registro"]["DATOS"]
    op = context.user_data["registro"]["OPERACION"]
    
    nombre_completo = NOMBRES_OPERACIONES.get(op, op)
    
    resumen = f"📋 *RESUMEN FINAL - {nombre_completo}*\n\n"
    for key, val in reg.items():
        if "FOTO" in key:
            resumen += f"📸 *{key.replace('_', ' ')}*: ✅ Subida\n"
        elif key not in ["MARCA"]: # Mostrar un resumen limpio
            resumen += f"🔸 *{key.replace('_', ' ')}*: `{val}`\n"
            
    kb = [
        [InlineKeyboardButton("💾 REGISTRAR DATOS", callback_data="FINAL_GUARDAR")], 
        [InlineKeyboardButton("❌ CANCELAR REGISTRO", callback_data="FINAL_CANCELAR")]
    ]
    await context.bot.send_message(chat_id, resumen + "\n¿Todo listo para finalizar?", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def manejar_resumen_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "FINAL_GUARDAR":
        await query.edit_message_text("💾 Procesando e inyectando datos de forma segura... ⏳")
        
        reg = context.user_data["registro"]["DATOS"]
        op = context.user_data["registro"]["OPERACION"]
        fecha_hora = datetime.now(ZONA_PERU).strftime("%Y-%m-%d %H:%M:%S")

        nombre_completo = NOMBRES_OPERACIONES.get(op, op)
        sn_antiguo_o_actual = reg.get("SN_ANTIGUO", reg.get("SN_ACTUAL", "-"))

        # 🔥 MAPEO EXACTO A LAS COLUMNAS USANDO LOS NUEVOS NOMBRES
        fila = [
            update.effective_user.id,                    # 0: USER_ID
            fecha_hora,                                  # 1: FECHA_HORA
            nombre_completo,                             # 2: TIPO_OPERACION
            reg.get("MARCA", "-"),                       # 3: MARCA
            reg.get("TICKET", "-"),                      # 4: TICKET
            reg.get("CODIGO CLIENTE", "-"),              # 5: COD_CLIENTE
            reg.get("CLIENTE", "-"),                     # 6: CLIENTE
            reg.get("DNI ó CE", "-"),                    # 7: DNI
            reg.get("DIRECCION", "-"),                   # 8: DIRECCION
            reg.get("DISTRITO", "-"),                    # 9: DISTRITO
            reg.get("PARTNER", "-"),                     # 10: CONTRATA
            reg.get("CUADRILLA", "-"),                   # 11: TECNICO
            reg.get("CTO", "-"),                         # 12: CTO
            reg.get("PUERTO UTILIZADO", "-"),            # 13: PUERTO_UTILIZADO
            sn_antiguo_o_actual,                         # 14: SN_ANTIGUO
            reg.get("SN_NUEVO", "-"),                    # 15: SN_NUEVO
            reg.get("PHONOWIN", "-"),                    # 16: PHONOWIN
            reg.get("PROD_ID", "-"),                     # 17: PROD_ID
            reg.get("MOTIVO DE REMATRICULACIÓN", "-"),   # 18: MOTIVO_REMAT
            reg.get("OBSERVACIÓN", "-")                  # 19: OBSERVACION
        ]
        
        fotos_procesadas = []
        for key, val in reg.items():
            if "FOTO" in key:
                abreviatura = key.replace("FOTO_", "")
                fotos_procesadas.append(f"{abreviatura}: {val}")
                
        fotos_fila = fotos_procesadas + ["-"] * (5 - len(fotos_procesadas))
        fila.extend(fotos_fila[:5])

        # 🔥 Orden: GESTOR, RECHAZO, SUBSANACION, POTENCIA, ESTADO, NOTIFICADO, FECHA_HORA FINALIZADO
        # 🔥 Orden ajustado: RECHAZO, SUBSANACION, POTENCIA, GESTOR, ESTADO, NOTIFICADO, FECHA FINALIZADO + 7 VACÍAS
        fila.extend(["-", "-", "-", "-","PENDIENTE REVISIÓN", "NO", "-", "-", "-", "-", "-", "-", "-", "-"])
        
        exito, msg = gs_append_row(fila)
        
        if exito:
            await query.edit_message_text("✅ Datos registrados exitosamente.")
            await context.bot.send_message(
                chat_id=query.message.chat_id, 
                text="✅ *¡Excelente! Registro exitoso.*\n\nTe notificaremos por aquí cuando el gestor revise la solicitud.", 
                parse_mode="Markdown"
            )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id, 
                text=f"⚠️ *Error al guardar:* {msg}",
                parse_mode="Markdown"
            )
            
    context.user_data.pop("registro", None)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("registro", None)
    await update.message.reply_text("❌ Registro cancelado de forma segura.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ======== MAIN ========
def main():
    validar_entorno_estricto()


# 🔥 Le subimos la "paciencia" al bot a 60 segundos para que descargue fotos pesadas sin llorar
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )

    job_queue = app.job_queue
    job_queue.run_repeating(verificar_cambios_estado, interval=10, first=5)

    # 🔥 NUEVO HANDLER: Atrapa las respuestas del botón de SUBSANAR
    subsanar_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_subsanacion, pattern="^SUBSANAR_")],
        states={
            RECIBIR_SUBSANACION: [MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.IMAGE, guardar_subsanacion)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(subsanar_handler)

    # HANDLER PRINCIPAL
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECCIONAR_OP: [CallbackQueryHandler(boton_operacion)],
            SELECCIONAR_MARCA: [CallbackQueryHandler(boton_marca)],
            PREGUNTAR_DATO: [
                CallbackQueryHandler(recibir_phonowin, pattern="^PHONO_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_texto),
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, recibir_foto)
            ],
            CONFIRMAR_DATO: [CallbackQueryHandler(manejar_confirmacion)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(manejar_resumen_final, pattern="^FINAL_"))
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
