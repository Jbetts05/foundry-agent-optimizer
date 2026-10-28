import azure.functions as func

from demo.http_api import create_app

middleware = func.AsgiMiddleware(create_app(serve_web=False))


async def main(req: func.HttpRequest, context: func.Context) -> func.HttpResponse:
    return await middleware.handle_async(req, context)
