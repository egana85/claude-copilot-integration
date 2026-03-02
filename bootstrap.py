#!/usr/bin/env python3
# =============================================================================
# bootstrap.py — Setup y validación del entorno Claude + Copilot Integration
# Ejecutar ANTES de cualquier integración de CI/CD (Punto de Control Semana 4)
# Uso: python bootstrap.py [--full-test] [--skip-redis]
# =============================================================================

import os
import sys
import json
import subprocess
import asyncio
import argparse
from pathlib import Path

# Force UTF-8 output on Windows (avoids UnicodeEncodeError with emoji/accents)
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ANSI colors
GREEN  = '\033[92m'
YELLOW = '\033[93m'
RED    = '\033[91m'
BLUE   = '\033[94m'
BOLD   = '\033[1m'
RESET  = '\033[0m'

def ok(msg: str)   -> None: print(f"  {GREEN}✅ {msg}{RESET}")
def warn(msg: str) -> None: print(f"  {YELLOW}⚠️  {msg}{RESET}")
def err(msg: str)  -> None: print(f"  {RED}❌ {msg}{RESET}")
def info(msg: str) -> None: print(f"  {BLUE}ℹ️  {msg}{RESET}")
def header(msg: str) -> None:
    print(f"\n{BOLD}{BLUE}{'-' * 60}{RESET}")
    print(f"{BOLD}{BLUE}  {msg}{RESET}")
    print(f"{BOLD}{BLUE}{'-' * 60}{RESET}")


# =============================================================================
# VERIFICACIONES
# =============================================================================

def check_python_version() -> bool:
    header("1. Python Version")
    major, minor = sys.version_info.major, sys.version_info.minor
    version_str = f"{major}.{minor}.{sys.version_info.micro}"
    if major == 3 and minor >= 10:
        ok(f"Python {version_str} ✓")
        return True
    else:
        err(f"Python {version_str} — Se requiere 3.10+")
        return False


def check_env_vars() -> bool:
    header("2. Variables de Entorno")
    required = ['ANTHROPIC_API_KEY']
    optional = {
        'DESIGN_MODEL':         'claude-opus-4-5 (default)',
        'REVIEW_MODEL':         'claude-haiku-4-5-20251001 (default)',
        'MAX_TOKENS_DESIGN':    '1500 (default)',
        'MAX_TOKENS_REVIEW':    '800 (default)',
        'REDIS_URL':            'redis://localhost:6379/0 (default)',
        'SYSTEM_PROMPT_PATH':   'system_prompt.txt (default)',
        'LOG_LEVEL':            'INFO (default)',
    }

    all_ok = True
    for var in required:
        val = os.environ.get(var)
        if val:
            masked = val[:8] + '...' + val[-4:] if len(val) > 12 else '***'
            ok(f"{var} = {masked}")
        else:
            err(f"{var} — NO DEFINIDA (requerida)")
            all_ok = False

    for var, default in optional.items():
        val = os.environ.get(var)
        if val:
            ok(f"{var} = {val}")
        else:
            warn(f"{var} no definida — usando {default}")

    return all_ok


def check_dependencies() -> bool:
    header("3. Dependencias Python")
    deps = {
        'anthropic':     'pip install anthropic',
        'redis':         'pip install redis[hiredis]',
    }
    optional_deps = {
        'watchdog':      'pip install watchdog  (opcional, para FileWatcher)',
    }

    all_ok = True
    for pkg, install_cmd in deps.items():
        try:
            mod = __import__(pkg)
            version = getattr(mod, '__version__', 'unknown')
            ok(f"{pkg} {version}")
        except ImportError:
            err(f"{pkg} NO instalado → {install_cmd}")
            all_ok = False

    for pkg, note in optional_deps.items():
        try:
            mod = __import__(pkg)
            version = getattr(mod, '__version__', 'unknown')
            ok(f"{pkg} {version} (opcional)")
        except ImportError:
            warn(f"{pkg} no instalado — {note}")

    return all_ok


def check_project_files() -> bool:
    header("4. Archivos del Proyecto")
    required_files = {
        'orchestrator.py':      'Orquestador principal',
        'ide_injector.py':      'Inyector de pseudocódigo',
        'system_prompt.txt':    'System prompt del proyecto',
    }
    optional_files = {
        '.github/workflows/claude-review.yml': 'GitHub Action para PR reviews',
        'requirements.txt':                    'Dependencias del proyecto',
        '.env':                                'Variables locales (NO commitear)',
    }

    all_ok = True
    for filename, desc in required_files.items():
        if Path(filename).exists():
            size = Path(filename).stat().st_size
            ok(f"{filename} ({size} bytes) — {desc}")
        else:
            err(f"{filename} NO encontrado — {desc}")
            all_ok = False

    for filename, desc in optional_files.items():
        if Path(filename).exists():
            ok(f"{filename} — {desc}")
        else:
            warn(f"{filename} no encontrado — {desc}")

    return all_ok


def check_system_prompt() -> bool:
    header("5. System Prompt")
    path = Path(os.environ.get('SYSTEM_PROMPT_PATH', 'system_prompt.txt'))

    if not path.exists():
        err(f"system_prompt.txt no encontrado en '{path}'")
        info("Crea el archivo con el stack y patrones de tu proyecto.")
        info("Ejemplo en: https://github.com/tu-org/claude-copilot-integration")
        return False

    content = path.read_text(encoding='utf-8')
    chars = len(content)
    words = len(content.split())

    ok(f"Archivo encontrado: {chars} caracteres, {words} palabras")

    # Verificar secciones clave
    checks = {
        'STACK':         'Stack tecnológico',
        'AL DISEÑAR':    'Instrucciones de diseño',
        'AL REVISAR':    'Instrucciones de revisión',
        'severity':      'Formato de severidad',
    }
    for keyword, desc in checks.items():
        if keyword in content:
            ok(f"  '{keyword}' presente — {desc}")
        else:
            warn(f"  '{keyword}' no encontrado — considera agregar: {desc}")

    # Estimar tokens aproximados (regla: 1 token ≈ 4 chars)
    estimated_tokens = chars // 4
    if estimated_tokens > 2000:
        warn(
            f"System prompt grande (~{estimated_tokens} tokens). "
            "El cache de Redis es especialmente importante aquí."
        )
    else:
        ok(f"Tamaño óptimo (~{estimated_tokens} tokens estimados)")

    return True


async def check_redis_connection(skip: bool = False) -> bool:
    header("6. Redis (Cache del System Prompt)")

    if skip:
        warn("Redis check omitido (--skip-redis). Se usará cache en memoria.")
        return True

    try:
        import redis.asyncio as redis_async
        redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
        r = redis_async.from_url(redis_url, socket_connect_timeout=2)
        await r.ping()
        info_data = await r.info('server')
        version = info_data.get('redis_version', 'unknown')
        ok(f"Redis conectado en {redis_url} (v{version})")
        await r.aclose()
        return True
    except ImportError:
        warn("redis package no instalado. Usando cache en memoria (menor ahorro de tokens).")
        return True
    except Exception as e:
        warn(f"Redis no disponible ({e})")
        warn("El orquestador funcionará con cache en memoria.")
        warn("Para activar Redis: docker run -d -p 6379:6379 redis:alpine")
        return True  # No bloquear — Redis es opcional


async def check_anthropic_api() -> bool:
    header("7. Conexión a Anthropic API")
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        err("ANTHROPIC_API_KEY no definida — skip de test de API")
        return False

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        # Llamada mínima de validación (1 token)
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=10,
            messages=[{'role': 'user', 'content': 'Responde solo: OK'}],
        )
        text = response.content[0].text.strip()
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens

        ok(f"API responde correctamente: '{text}'")
        ok(f"Tokens usados: {input_tokens}in + {output_tokens}out")
        return True

    except anthropic.AuthenticationError:
        err("API Key inválida. Verifica ANTHROPIC_API_KEY.")
        return False
    except anthropic.RateLimitError:
        warn("Rate limit alcanzado en el test. La API está configurada correctamente.")
        return True
    except Exception as e:
        err(f"Error conectando a Anthropic API: {e}")
        return False


async def run_full_test() -> bool:
    header("8. Test End-to-End (Ciclo Completo)")

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        err("ANTHROPIC_API_KEY no disponible — skip del test E2E")
        return False

    # Crear archivo de prueba temporal (compatible Windows/Unix)
    import tempfile
    _tmp = Path(tempfile.gettempdir())
    test_file = _tmp / 'test_feature.py'
    test_file.write_text(
        "# test_feature.py — archivo de prueba para el ciclo Claude + Copilot\n\n",
        encoding='utf-8',
    )

    test_diff = """diff --git a/app/service.py b/app/service.py
--- a/app/service.py
+++ b/app/service.py
@@ -1,3 +1,10 @@
+def create_user(db, email, password):
+    user = db.query(User).filter(User.email == email).first()
+    if user:
+        return None
+    new_user = User(email=email, password=password)
+    db.add(new_user)
+    db.commit()
+    return new_user
"""

    try:
        # PASO 1: Design
        info("Paso 1/3: Probando design_feature()...")
        result = subprocess.run(
            [sys.executable, 'orchestrator.py', 'design',
             'Crear endpoint REST para registro de usuario con validación de email duplicado'],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'ANTHROPIC_API_KEY': api_key},
        )

        if result.returncode == 0 and result.stdout.strip():
            pseudo_lines = len(result.stdout.strip().splitlines())
            ok(f"design_feature() OK → {pseudo_lines} líneas de pseudocódigo generadas")

            # PASO 2: Inyección
            info("Paso 2/3: Probando IDEInjector.inject_pseudocode()...")
            try:
                # Importar directamente para el test
                sys.path.insert(0, '.')
                from ide_injector import IDEInjector, InjectionRequest, InjectionMode
                injector = IDEInjector()
                req = InjectionRequest(
                    pseudocode=result.stdout,
                    target_file=str(test_file),
                    mode=InjectionMode.APPEND,
                    tag='test-bootstrap',
                )
                inj_result = injector.inject(req)
                if inj_result.success:
                    ok(f"IDEInjector OK → {inj_result.lines_injected} líneas inyectadas en {test_file}")
                else:
                    warn(f"IDEInjector warning: {inj_result.error}")
            except Exception as e:
                warn(f"IDEInjector no disponible aún: {e}")
        else:
            warn(f"design_feature() retornó código {result.returncode}")
            if result.stderr:
                info(f"stderr: {result.stderr[:200]}")

        # PASO 3: Review
        info("Paso 3/3: Probando review_code()...")
        diff_file = _tmp / 'test.diff'
        diff_file.write_text(test_diff, encoding='utf-8')

        result2 = subprocess.run(
            [sys.executable, 'orchestrator.py', 'review', str(diff_file)],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'ANTHROPIC_API_KEY': api_key},
        )

        if result2.returncode == 0:
            try:
                issues = json.loads(result2.stdout)
                ok(f"review_code() OK → {len(issues)} issue(s) encontrados")
                for issue in issues[:3]:
                    sev = issue.get('severity', '?')
                    msg = issue.get('issue', '')[:60]
                    info(f"  [{sev}] {msg}")
            except json.JSONDecodeError:
                warn(f"review_code() no retornó JSON válido: {result2.stdout[:100]}")
        else:
            warn(f"review_code() retornó código {result2.returncode}")

        return True

    except subprocess.TimeoutExpired:
        warn("Test E2E timeout (30s). La API puede estar lenta.")
        return False
    except Exception as e:
        err(f"Error en test E2E: {e}")
        return False
    finally:
        # Limpiar archivos temporales
        for f in [test_file, _tmp / 'test.diff']:
            if f.exists():
                f.unlink()


def print_summary(results: dict[str, bool]) -> None:
    header("RESUMEN")
    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for check, status in results.items():
        icon = "✅" if status else "❌"
        color = GREEN if status else RED
        print(f"  {color}{icon} {check}{RESET}")

    print(f"\n{BOLD}Resultado: {passed}/{total} verificaciones pasadas{RESET}")

    if passed == total:
        print(f"\n{GREEN}{BOLD}🚀 Entorno listo. Puedes proceder con la integración CI/CD.{RESET}")
    elif passed >= total - 2:
        print(
            f"\n{YELLOW}{BOLD}⚠️  Entorno funcional con advertencias. "
            f"Revisa los ítems marcados con ❌ antes del CI/CD.{RESET}"
        )
    else:
        print(
            f"\n{RED}{BOLD}❌ Entorno incompleto. "
            f"Resuelve los errores antes de continuar.{RESET}"
        )
        print(f"\n{YELLOW}Pasos de instalación rápida:{RESET}")
        print("  pip install anthropic redis[hiredis]")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        print("  cp system_prompt.txt . # si no existe")


# =============================================================================
# MAIN
# =============================================================================

async def main() -> None:
    parser = argparse.ArgumentParser(
        description='Bootstrap y validación del entorno Claude + Copilot Integration'
    )
    parser.add_argument(
        '--full-test', action='store_true',
        help='Ejecutar test end-to-end completo (consume tokens reales)',
    )
    parser.add_argument(
        '--skip-redis', action='store_true',
        help='Omitir verificación de Redis',
    )
    args = parser.parse_args()

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Claude + Copilot Integration — Bootstrap v1.0{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")

    results: dict[str, bool] = {}

    results['Python 3.10+']         = check_python_version()
    results['Variables de entorno'] = check_env_vars()
    results['Dependencias']         = check_dependencies()
    results['Archivos del proyecto']= check_project_files()
    results['System prompt']        = check_system_prompt()
    results['Redis']                = await check_redis_connection(skip=args.skip_redis)
    results['Anthropic API']        = await check_anthropic_api()

    if args.full_test:
        results['Test E2E']         = await run_full_test()

    print_summary(results)

    # Exit code para CI
    critical_checks = ['Python 3.10+', 'Variables de entorno', 'Anthropic API']
    if not all(results.get(c, False) for c in critical_checks):
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
