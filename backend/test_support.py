from fastapi import FastAPI


def domain_app(source):
    """Exercise domain routes separately from the authentication integration suite."""
    app = FastAPI()
    app.router.routes.extend(source.router.routes)
    return app
