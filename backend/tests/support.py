from fastapi import FastAPI


def migrate_test_database(config):
    """Use the same Flyway entry point as deployment, before opening the app."""
    from backend.migrations.runner import run_flyway
    run_flyway(config, "migrate", capture=True)


def domain_app(source):
    """Exercise domain routes separately from the authentication integration suite."""
    app = FastAPI()
    app.router.routes.extend(source.router.routes)
    return app
