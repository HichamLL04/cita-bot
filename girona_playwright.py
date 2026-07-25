import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    print("Playwright no está instalado. Para instalarlo en tu servidor Ubuntu o PC local:")
    print("  pip install playwright")
    print("  playwright install chromium")
    sys.exit(1)

import requests

# Configuración de logging detallado
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
    """Calcula el intervalo dinámico en segundos según la hora y día actual."""
    if not config.get("use_smart_scheduling", True):
        normal = config.get("check_interval_seconds", 900)
        return normal, "Modo Fijo"

    now = datetime.now()
    weekday = now.weekday()
    hour = now.hour
    minute = now.minute

    is_peak = False
    if 0 <= weekday <= 4:
        if (hour == 7 and minute >= 45) or (hour == 8) or (hour == 9 and minute <= 30):
            is_peak = True

    if weekday == 3:
        if (hour == 14 and minute >= 30) or (hour == 15):
            is_peak = True

    if is_peak:
        interval = config.get("peak_interval_seconds", 90)
        return interval, "🔥 HORA PUNTA (Búsqueda Rápida cada 90s)"
    else:
        interval = config.get("off_peak_interval_seconds", 900)
        return interval, "💤 Hora Normal (Búsqueda cada 15m)"

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
        logging.warning("⚠️ No se ha configurado 'anticaptcha_api_key' en config.json. Omitiendo...")
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

def print_page_details(page, step_label: str) -> None:
    """Imprime en el log la información detallada de la página actual."""
    try:
        url = page.url
        title = page.title()
        details = page.evaluate("""() => {
            const selects = Array.from(document.querySelectorAll('select')).map(s => ({
                id: s.id, name: s.name, optionsCount: s.options.length, firstOpt: s.options[0]?.text
            }));
            const buttons = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], a.btn, a.mf-button')).map(b => ({
                id: b.id, name: b.name, value: b.value || b.innerText.trim()
            }));
            const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])')).map(i => ({
                id: i.id, name: i.name, type: i.type
            }));
            return { selects, buttons, inputs };
        }""")
        
        logging.info(f"📄 [{step_label}] PÁGINA ACTUAL: '{title}' | URL: {url}")
        logging.info(f"   └─ Selects ({len(details['selects'])}): {details['selects']}")
        logging.info(f"   └─ Botones ({len(details['buttons'])}): {details['buttons']}")
        logging.info(f"   └─ Campos Inputs ({len(details['inputs'])}): {details['inputs']}")
    except Exception as e:
        logging.warning(f"   Error leyendo detalles de la página [{step_label}]: {e}")

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
    Realiza una comprobación de citas con cabeceras HTTP reales para evitar la regla WAF 'Intrusion Prevention Violation'.
    """
    url_portal = "https://icp.administracionelectronica.gob.es/icpplus/index.html"
    ntfy_topic = config.get("ntfy_topic", "")
    TIMEOUT = 60000

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
        
        # Cabeceras HTTP reales de navegador Chrome completo para pasar el Firewall/WAF
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
                "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1"
            }
        )

        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.set_default_timeout(TIMEOUT)

        try:
            t0 = time.time()
            logging.info(f"PASO 1: Conectando al portal oficial ({url_portal})...")
            
            page.goto(url_portal, wait_until="load", timeout=TIMEOUT)
            print_page_details(page, "PASO 1")

            page_title_lower = page.title().lower()
            page_text_init = page.content().lower()
            
            block_keywords = [
                "intrusion prevention violation",
                "acceso restringido",
                "exceso de peticiones",
                "demasiadas peticiones",
                "403 forbidden",
                "bloqueo temporal",
                "fortinet",
                "access denied"
            ]

            if any(bk in page_text_init or bk in page_title_lower for bk in block_keywords):
                logging.warning("⚠️ PASO 1 FAILED: Detectada página de bloqueo WAF/Firewall ('Intrusion Prevention Violation').")
                browser.close()
                return False, True

            # Paso 2: Seleccionar Provincia (Girona)
            t1 = time.time()
            logging.info("PASO 2: Buscando selector de provincia 'Girona'...")
            
            selected_prov = False
            for sel in ["#form", "#provincia", "select[name='form']", "select"]:
                if page.is_visible(sel):
                    try:
                        page.select_option(sel, label="Girona")
                        selected_prov = True
                        logging.info(f"   [OK] Provincia 'Girona' seleccionada en selector '{sel}'.")
                        break
                    except Exception as e_sel:
                        logging.warning(f"   Intento de selección en '{sel}' falló: {e_sel}")

            if not selected_prov:
                # Si no está visible inmediatamente, buscar si hay botón de entrada en portada
                for b_sel in ["#btnEnviar", "#btnEntrar", "#btnAceptar"]:
                    if page.is_visible(b_sel):
                        logging.info(f"   Pulsando botón de entrada '{b_sel}' en portada...")
                        page.click(b_sel)
                        time.sleep(2)
                        print_page_details(page, "PASO 2 (post-click entrada)")
                        break
                
                # Reintentar selección
                for sel in ["#form", "#provincia", "select[name='form']", "select"]:
                    if page.is_visible(sel):
                        page.select_option(sel, label="Girona")
                        selected_prov = True
                        break

            if not selected_prov:
                raise Exception("No se encontró el desplegable de provincias en la página.")

            logging.info("   Pulsando 'Aceptar' (#btnAceptar o #btnEnviar)...")
            btn_step2 = "#btnAceptar" if page.is_visible("#btnAceptar") else "#btnEnviar"
            page.click(btn_step2)
            
            time.sleep(2)
            print_page_details(page, "PASO 2 (después de elegir Girona)")

            # Paso 3: Seleccionar Oficina y Trámite
            use_any_office = config.get("use_any_office", True)
            tramite_id = config.get("tramite_id", "4010")

            if use_any_office:
                logging.info("PASO 3: Seleccionando 'Cualquier oficina' (opción 99)...")
                if page.is_visible("#sede"):
                    page.select_option("#sede", value="99")
            else:
                offices = config.get("offices", [{"id": "4", "name": "CNP LLORET DE MAR"}])
                if offices and page.is_visible("#sede"):
                    logging.info(f"PASO 3: Seleccionando oficina ID {offices[0].get('id')}...")
                    page.select_option("#sede", value=offices[0].get("id", "4"))

            tramite_selected = False
            for select_name in ["tramiteGrupo[1]", "tramiteGrupo[0]", "tramiteCuerpo[0]", "tramite[0]"]:
                if page.is_visible(f"select[name='{select_name}']"):
                    try:
                        page.select_option(f"select[name='{select_name}']", value=tramite_id)
                        tramite_selected = True
                        logging.info(f"   [OK] Trámite {tramite_id} seleccionado en {select_name}.")
                        break
                    except Exception as e_tr:
                        logging.warning(f"   Intento selección trámite en {select_name} falló: {e_tr}")
            
            if not tramite_selected and page.is_visible("#tramite"):
                page.select_option("#tramite", value=tramite_id)
                logging.info(f"   [OK] Trámite {tramite_id} seleccionado en #tramite.")

            logging.info("   Pulsando 'Aceptar' (#btnAceptar)...")
            t2 = time.time()
            btn_step3 = "#btnAceptar" if page.is_visible("#btnAceptar") else "#btnEnviar"
            page.click(btn_step3)
            time.sleep(2)
            print_page_details(page, "PASO 3 (post-trámite)")

            # Paso 4: Pantalla de Información / Instrucciones (#btnEntrar)
            if page.is_visible("#btnEntrar"):
                logging.info("PASO 4: Pantalla de instrucciones detectada. Pulsando 'Entrar' (#btnEntrar)...")
                page.click("#btnEntrar")
                time.sleep(2)
                print_page_details(page, "PASO 4 (post-entrar)")

            # Paso 5: Rellenar Formulario de Datos Personales
            logging.info("PASO 5: Rellenando NIE, Nombre y País...")
            
            doc_type = config.get("doc_type", "NIE").upper()
            if doc_type == "NIE" and page.is_visible("#rbtNie"):
                page.check("#rbtNie")
            elif doc_type == "PASAPORTE" and page.is_visible("#rbtPasaporte"):
                page.check("#rbtPasaporte")
            elif page.is_visible("#rbtNif"):
                page.check("#rbtNif")

            page.fill("#txtIdCitador", config.get("doc_value", ""))
            page.fill("#txtDesCitador", config.get("name", ""))

            country = config.get("country", "MARRUECOS")
            if page.is_visible("#txtPaisNac"):
                try:
                    page.select_option("#txtPaisNac", label=country.upper())
                    logging.info(f"   [OK] País seleccionado: {country.upper()}")
                except Exception:
                    try:
                        page.select_option("#txtPaisNac", value="348")
                        logging.info("   [OK] País seleccionado por código 348.")
                    except Exception as e_c:
                        logging.warning(f"   No se pudo seleccionar país '{country}': {e_c}")

            handle_captcha_if_present(page, config)

            logging.info("   Pulsando 'Enviar' (#btnEnviar)...")
            t3 = time.time()
            page.click("#btnEnviar")
            time.sleep(2)
            print_page_details(page, "PASO 5 (post-formulario)")

            # Paso 6: Consultar Cita
            if page.is_visible("#btnEnviar"):
                handle_captcha_if_present(page, config)
                logging.info("PASO 6: Pulsando 'Solicitar Cita' (#btnEnviar)...")
                page.click("#btnEnviar")
                time.sleep(2)
                print_page_details(page, "PASO 6 (resultado final)")

            logging.info(f"   [OK] Consulta completada en {time.time() - t3:.2f}s. Analizando respuesta...")
            page_text = page.content().lower()

            if any(bk in page_text for bk in block_keywords):
                logging.warning("⚠️ Detectada respuesta de bloqueo o sobrecarga.")
                browser.close()
                return False, True

            no_available_keywords = [
                "en este momento no hay citas disponibles",
                "no hay citas disponibles",
                "sin citas disponibles",
                "el servicio se encuentra sobrecargado"
            ]

            has_no_appointments = any(kw in page_text for kw in no_available_keywords)

            preferred_offices = config.get("offices", [])
            preferred_ids = [str(o.get("id")) for o in preferred_offices if "id" in o]

            if page.is_visible("#idSede"):
                options = page.eval_on_selector_all("#idSede option", """
                    opts => opts.map(o => ({ value: o.value, text: o.innerText.trim() }))
                """)
                
                available_offices = [o for o in options if o['value'] and o['value'] != ""]

                if available_offices:
                    matching_offices = []
                    if preferred_ids:
                        for o in available_offices:
                            if str(o['value']) in preferred_ids:
                                matching_offices.append(o)
                    else:
                        matching_offices = available_offices

                    only_preferred = config.get("only_notify_preferred_offices", True)

                    if matching_offices:
                        offices_str = "\n".join([f"• {o['text']}" for o in matching_offices])
                        msg = (
                            f"🚨 ¡¡¡ CITA DISPONIBLE EN TU OFICINA PREFERIDA !!! 🚨\n\n"
                            f"Oficinas deseadas con hueco libre:\n{offices_str}\n\n"
                            f"Trámite: Toma de Huellas ({tramite_id})\n"
                            f"¡Entra de inmediato a la Sede Electrónica!"
                        )
                        logging.info(msg)
                        send_ntfy_alert(
                            topic=ntfy_topic,
                            title="🚨 Cita Disponible en Tu Oficina!",
                            message=msg,
                            priority="urgent"
                        )
                        page.screenshot(path="cita_disponible_girona.png")
                        browser.close()
                        return True, False
                    
                    elif not only_preferred:
                        offices_str = "\n".join([f"• {o['text']}" for o in available_offices])
                        msg = (
                            f"ℹ️ Cita disponible en otras oficinas de Girona:\n{offices_str}"
                        )
                        logging.info(msg)
                        send_ntfy_alert(
                            topic=ntfy_topic,
                            title="ℹ️ Cita Disponible (Otra Oficina)",
                            message=msg,
                            priority="default"
                        )
                    else:
                        logging.info("   Hay citas en la provincia, pero NINGUNA coincide con tus oficinas preferidas (Lloret / Grober / Jaume I). Ignorando alerta.")

            elif not has_no_appointments:
                msg = (
                    f"🚨 ¡¡¡ CITA PREVIA DISPONIBLE EN GIRONA !!! 🚨\n\n"
                    f"Trámite: Toma de Huellas ({tramite_id})\n"
                    f"¡Entra inmediatamente a la Sede Electrónica!"
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
                logging.info("   RESULTADO: Sin citas disponibles en ninguna oficina de la provincia en este momento.")

        except PlaywrightTimeoutError as te:
            logging.warning(f"❌ TIMEOUT EXCEPCIÓN en URL '{page.url}': {te}")
            try:
                page.screenshot(path="error_timeout.png")
                with open("error_timeout.html", "w", encoding="utf-8") as fh:
                    fh.write(page.content())
                logging.info("   [Diagnóstico] Captura guardada en 'error_timeout.png' y HTML en 'error_timeout.html'.")
            except Exception:
                pass
        except Exception as ex:
            logging.error(f"❌ EXCEPCIÓN INESPERADA en URL '{page.url}': {ex}")
            logging.error(traceback.format_exc())
            try:
                page.screenshot(path="error_exception.png")
            except Exception:
                pass
        finally:
            browser.close()

    return False, False

def main():
    config = load_config("config.json")
    pause_hours = config.get("pause_hours_on_block", 1.0)
    ntfy_topic = config.get("ntfy_topic", "")
    headless_mode = "--no-headless" not in sys.argv

    logging.info("=== Monitor de Cita Previa Girona (Evasión WAF) ===")
    if ntfy_topic:
        logging.info(f"Notificaciones ntfy.sh activas en: https://ntfy.sh/{ntfy_topic}")

    attempt = 1
    consecutive_errors = 0

    while True:
        interval, schedule_mode = get_smart_check_interval(config)
        logging.info(f"--- Intento #{attempt} | Estado: {schedule_mode} ---")
        
        found, is_blocked = check_cita_girona(config, headless=headless_mode)
        
        if found:
            logging.info("¡Cita encontrada en oficina preferida! Deteniendo bucle para que procedas a confirmar.")
            break

        if is_blocked:
            consecutive_errors += 1
            if consecutive_errors >= 2 or is_blocked:
                resume_time = datetime.now() + timedelta(hours=pause_hours)
                resume_str = resume_time.strftime("%H:%M:%S")
                msg_pause = f"⚠️ Sede Electrónica restringida (Firewall). Pausa de {pause_hours} hora(s). Reanudación a las {resume_str}."
                logging.warning(msg_pause)
                send_ntfy_alert(
                    topic=ntfy_topic,
                    title="⚠️ Servidor en Pausa (Firewall)",
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
