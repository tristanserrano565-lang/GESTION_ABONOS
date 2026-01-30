from gestion_abonos_app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True)

#HAY QUE METER VALIDACIONES!!! AHORA MISMO SE PERMITE ASIGNAR EL MISMO ABONO EL MISMO PARTIDO MAS DE UNA VEZ EN TODOS LOS NIVELES