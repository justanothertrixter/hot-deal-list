import os


bind = "0.0.0.0:" + os.environ.get("PORT", "8000")
workers = 1          # 1 воркер: планировщик не должен дублироваться
threads = 8
timeout = 300
keepalive = 5


def post_fork(server, worker):
    """Запускаем фоновый сбор и планировщик в воркере (а не в main(), который gunicorn не вызывает)."""
    if os.environ.get("HOTDEAL_DISABLE_SCHEDULER") == "1":
        return
    import threading
    import app
    threading.Thread(target=app.startup_collect, daemon=True).start()
    threading.Thread(target=app.scheduler_loop, daemon=True).start()
