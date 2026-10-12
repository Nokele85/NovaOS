# Authenticated LAN console, same raw REPL as USB (SPEC §26.6).
# Access is configured by the owner in /nova_remote.json, never in source.
def start():
    import json
    import webrepl
    try:
        with open('/nova_remote.json') as f:
            config = json.load(f)
    except (OSError, ValueError):
        return False
    password = config.get('webrepl_password')
    if not config.get('enabled') or not password:
        return False
    webrepl.start(password=password)
    return True
