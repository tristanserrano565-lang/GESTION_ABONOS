from werkzeug.serving import WSGIRequestHandler

from gestion_abonos_app import create_app


class HardenedRequestHandler(WSGIRequestHandler):
    server_version = "Servidor"
    sys_version = ""

    def version_string(self):
        return self.server_version


app = create_app()

if __name__ == "__main__":
    app.run(debug=False, request_handler=HardenedRequestHandler)
