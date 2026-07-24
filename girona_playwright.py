import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    print("Playwright no está instalado. Para instalarlo en tu servidor Ubuntu o PC local:")
    print("  pip install playwright")
    print("  playwright install chromium")
    sys.exit(1)

import requests

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("cita_girona.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

def send_ntfy_alert(topic: str, title: str, message: str, priority: str = "high") -> None:
    """Envía notificación push instantánea al móvil a través de ntfy.sh."""
    if not topic:
        logging.warning("⚠️ No se ha especificado 'ntfy_topic' en config.json. Imprimiendo en consola:")
        logging.info(f"[{title}] {message}")
        return

    url = f"https://ntfy.sh/{topic}"
    try:
        res = requests.post(
            url,
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": "rotating_light,warning"
            },
            timeout=10
        )
        if res.status_code == 200:
            logging.info(f"🔔 Notificación enviada con éxito a ntfy.sh/{topic}")
        else:
            logging.warning(f"ntfy.sh devolvió código {res.status_code}: {res.text}")
    except Exception as e:
        logging.error(f"Error enviando notificación a ntfy.sh: {e}")

def get_smart_check_interval(config: Dict[str, Any]) -> Tuple[int, str]:
    """
    Calcula el intervalo dinámico en segundos según la hora y día actual:
    - Horas punta (Lunes a Viernes 07:45 - 09:30, y Jueves 14:30 - 16:00): Búsqueda rápida (ej: 90s).
    - Horas normales: Búsqueda cada 10-15 minutos (ej: 900s).
    """
    if not config.get("use_smart_scheduling", True):
        normal = config.get("check_interval_seconds", 900)
        return normal, "Modo Fijo"

    now = datetime.now()
    weekday = now.weekday() # 0 = Lunes, 3 = Jueves, 6 = Domingo
    hour = now.hour
    minute = now.minute

    is_peak = False

    # Lunes a Viernes de 07:45 a 09:30
    if 0 <= weekday <= 4:
        if (hour == 7 and minute >= 45) or (hour == 8) or (hour == 9 and minute <= 30):
            is_peak = True

    # Jueves de 14:30 a 16:00
    if weekday == 3:
        if (hour == 14 and minute >= 30) or (hour == 15):
            is_peak = True

    if is_peak:
        interval = config.get("peak_interval_seconds", 90)
        return interval, "🔥 HORA PUNTA (Búsqueda Rápida)"
    else:
        interval = config.get("off_peak_interval_seconds", 900)
        return interval, "💤 Hora Normal (Búsqueda cada 10-15m)"

def solve_recaptcha_anticaptcha(api_key: str, page_url: str, site_key: str) -> Optional[str]:
    """Resuelve reCAPTCHA v2 usando la API de Anti-Captcha.com."""
    logging.info("Enviando reCAPTCHA a Anti-Captcha...")
    try:
        create_task_url = "https://api.anti-captcha.com/createTask"
        payload = {
            "clientKey": api_key,
            "task": {
                "type": "NoCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key
            }
        }
        res = requests.post(create_task_url, json=payload, timeout=30).json()
        if res.get("errorId") != 0:
            logging.error(f"Error Anti-Captcha: {res.get('errorDescription')}")
            return None
        
        task_id = res.get("taskId")
        logging.info(f"Tarea Anti-Captcha creada con ID: {task_id}. Esperando resolución...")

        get_result_url = "https://api.anti-captcha.com/getTaskResult"
        for _ in range(24):
            time.sleep(5)
            check_res = requests.post(get_result_url, json={"clientKey": api_key, "taskId": task_id}, timeout=30).json()
            if check_res.get("status") == "ready":
                token = check_res.get("solution", {}).get("gRecaptchaResponse")
                logging.info("¡reCAPTCHA resuelto con éxito!")
                return token
            elif check_res.get("errorId") != 0:
                logging.error(f"Error durante resolución: {check_res.get('errorDescription')}")
                return None
    except Exception as e:
        logging.error(f"Excepción resolviendo captcha: {e}")
    return None

def handle_captcha_if_present(page, config: Dict[str, Any]) -> None:
    """Detecta y resuelve el reCAPTCHA si está presente en la página actual."""
    api_key = config.get("anticaptcha_api_key")
    
    g_recaptcha = page.query_selector(".g-recaptcha, iframe[src*='recaptcha']")
    if not g_recaptcha:
        return

    logging.info("🔒 reCAPTCHA detectado en la página.")
    
    if not api_key:
        logging.warning("⚠️ No se ha configurado 'anticaptcha_api_key' en config.json. Intentando omitir...")
        return

    site_key = page.evaluate("""() => {
        const el = document.querySelector('.g-recaptcha');
        if (el) return el.getAttribute('data-sitekey');
        const iframe = document.querySelector("iframe[src*='recaptcha']");
        if (iframe) {
            const match = iframe.src.match(/[?&]k=([^&]+)/);
            return match ? match[1] : null;
        }
        return null;
    }""")

    if not site_key:
        logging.warning("No se pudo extraer la clave sitekey del reCAPTCHA.")
        return

    token = solve_recaptcha_anticaptcha(api_key, page.url, site_key)
    if token:
        page.evaluate(f"""(token) => {{
            const el = document.getElementById('g-recaptcha-response');
            if (el) {{
                el.value = token;
                el.innerHTML = token;
            }}
            if (typeof onSubmit === 'function') {{
                onSubmit(token);
            }}
        }}""", token)
        time.sleep(1)

def load_config(config_path: str = "config.json") -> Dict[str, Any]:
    """Carga la configuración local desatendida sin exponer datos en el repositorio."""
    if not os.path.exists(config_path):
        example_path = "config.example.json"
        logging.error(
            f"No se encontró el archivo '{config_path}'.\n"
            f"Por favor, copia '{example_path}' a '{config_path}' y completa tus datos privados ahí."
        )
        sys.exit(1)
    
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

def check_cita_girona(config: Dict[str, Any], headless: bool = True) -> Tuple[bool, bool]:
    """
    Realiza un intento de comprobación de citas en Girona.
    Retorna: (cita_encontrada: bool, requiere_pausa_bloqueo: bool)
    """
    url = "https://icp.administracionelectronica.gob.es/icpplus/index.html"
    ntfy_topic = config.get("ntfy_topic", "")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.set_default_timeout(25000)

        try:
            logging.info("1. Conectando al portal de Cita Previa...")
            page.goto(url)

            page_text_init = page.content().lower()
            block_keywords = [
                "acceso restringido",
                "exceso de peticiones",
                "demasiadas peticiones",
                "403 forbidden",
                "bloqueo temporal"
            ]
            if any(bk in page_text_init for bk in block_keywords):
                logging.warning("⚠️ Detectado aviso de bloqueo o restricción de la Sede.")
                browser.close()
                return False, True

            # Paso 1: Seleccionar Provincia (Girona)
            logging.info("2. Seleccionando provincia Girona...")
            page.select_option("#form", label="Girona")
            page.click("#btnAceptar")

            # Paso 2: Seleccionar Oficina y Trámite
            offices = config.get("offices", [{"id": "4", "name": "CNP LLORET DE MAR"}])
            tramite_id = config.get("tramite_id", "4010")

            for office in offices:
                office_id = office.get("id")
                office_name = office.get("name")
                logging.info(f"3. Probando oficina: {office_name} (ID: {office_id})...")

                if page.is_visible("#sede"):
                    page.select_option("#sede", value=office_id)
                    time.sleep(1)

                tramite_selected = False
                for select_name in ["tramiteGrupo[0]", "tramiteGrupo[1]", "tramiteCuerpo[0]", "tramite[0]"]:
                    if page.is_visible(f"select[name='{select_name}']"):
                        try:
                            page.select_option(f"select[name='{select_name}']", value=tramite_id)
                            tramite_selected = True
                            logging.info(f"   Trámite {tramite_id} seleccionado.")
                            break
                        except Exception:
                            continue
                
                if not tramite_selected and page.is_visible("#tramite"):
                    page.select_option("#tramite", value=tramite_id)

                page.click("#btnAceptar")

                if page.is_visible("#btnEntrar"):
                    page.click("#btnEntrar")

                # Paso 4: Rellenar Formulario de Datos Personales
                logging.info("4. Rellenando formulario de datos...")
                
                doc_type = config.get("doc_type", "NIE").upper()
                if doc_type == "NIE" and page.is_visible("#rbtNie"):
                    page.check("#rbtNie")
                elif doc_type == "PASAPORTE" and page.is_visible("#rbtPasaporte"):
                    page.check("#rbtPasaporte")
                elif page.is_visible("#rbtNif"):
                    page.check("#rbtNif")

                page.fill("#txtIdCitador", config.get("doc_value", ""))
                page.fill("#txtDesCitador", config.get("name", ""))

                handle_captcha_if_present(page, config)

                page.click("#btnEnviar")

                # Paso 5: Consultar Disponibilidad Cita
                if page.is_visible("#btnEnviar"):
                    handle_captcha_if_present(page, config)
                    page.click("#btnEnviar")

                page_text = page.content().lower()
                
                if any(bk in page_text for bk in block_keywords):
                    logging.warning(f"⚠️ Detectada respuesta de bloqueo en {office_name}.")
                    browser.close()
                    return False, True

                no_available_keywords = [
                    "en este momento no hay citas disponibles",
                    "no hay citas disponibles",
                    "sin citas disponibles",
                    "el servicio se encuentra sobrecargado"
                ]

                has_no_appointments = any(kw in page_text for kw in no_available_keywords)

                if not has_no_appointments:
                    msg = (
                        f"🚨 ¡¡¡ CITA PREVIA DISPONIBLE EN GIRONA !!! 🚨\n\n"
                        f"Oficina: {office_name}\n"
                        f"Trámite: Toma de Huellas ({tramite_id})\n"
                        f"Entra de inmediato a la Sede Electrónica."
                    )
                    logging.info(msg)
                    send_ntfy_alert(
                        topic=ntfy_topic,
                        title="🚨 Cita Previa Disponible en Girona!",
                        message=msg,
                        priority="urgent"
                    )
                    
                    page.screenshot(path="cita_disponible_girona.png")
                    browser.close()
                    return True, False
                else:
                    logging.info(f"   Sin citas disponibles en {office_name}.")
                
                page.goto("https://icp.administracionelectronica.gob.es/icpplus/citar?locale=es")
                time.sleep(2)

        except PlaywrightTimeoutError:
            logging.warning("Timeout durante la navegación. Puede que el servidor de la Sede esté lento.")
        except Exception as e:
            logging.error(f"Error durante el intento: {e}")
        finally:
            browser.close()

    return False, False

def main():
    config = load_config("config.json")
    pause_hours = config.get("pause_hours_on_block", 1.0)
    ntfy_topic = config.get("ntfy_topic", "")
    headless_mode = "--no-headless" not in sys.argv

    logging.info("=== Monitor de Cita Previa Girona (Playwright + ntfy.sh) ===")
    if ntfy_topic:
        logging.info(f"Notificaciones ntfy.sh activas en el canal: https://ntfy.sh/{ntfy_topic}")

    attempt = 1
    consecutive_errors = 0

    while True:
        interval, schedule_mode = get_smart_check_interval(config)
        logging.info(f"--- Intento #{attempt} | Estado: {schedule_mode} ---")
        
        found, is_blocked = check_cita_girona(config, headless=headless_mode)
        
        if found:
            logging.info("¡Cita encontrada! Deteniendo bucle para que procedas a confirmar.")
            break

        if is_blocked:
            consecutive_errors += 1
            if consecutive_errors >= 2 or is_blocked:
                resume_time = datetime.now() + timedelta(hours=pause_hours)
                resume_str = resume_time.strftime("%H:%M:%S")
                msg_pause = f"⚠️ Sede Electrónica restringida. Pausa de {pause_hours} hora(s). Reanudación a las {resume_str}."
                logging.warning(msg_pause)
                send_ntfy_alert(
                    topic=ntfy_topic,
                    title="⚠️ Servidor en Pausa",
                    message=msg_pause,
                    priority="default"
                )
                
                time.sleep(int(pause_hours * 3600))
                consecutive_errors = 0
                attempt += 1
                continue
        else:
            consecutive_errors = 0

        logging.info(f"Esperando {interval // 60}m {interval % 60}s ({interval}s) para el siguiente intento...")
        time.sleep(interval)
        attempt += 1

if __name__ == "__main__":
    main()
