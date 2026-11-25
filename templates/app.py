import os
import uuid
import sqlite3
import secrets
import unicodedata
from flask import jsonify
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, abort)
from werkzeug.security import generate_password_hash, check_password_hash
from database import conectar, crear_tablas
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get("FRAG_SECRET_KEY", secrets.token_hex(32))

app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024   # fotos de máximo 5 MB

CARPETA_IMG = os.path.join(app.root_path, "static", "img")
EXTENSIONES = {"jpg", "jpeg", "png", "webp"}
CATEGORIAS = ["mujer", "hombre", "unisex"]


def guardar_imagen(archivo):
    """Guarda la foto y devuelve su nombre. Devuelve None si no hay foto válida."""
    if not archivo or archivo.filename == "":
        return None
    if "." not in archivo.filename:
        return None
    extension = archivo.filename.rsplit(".", 1)[1].lower()
    if extension not in EXTENSIONES:
        return None
    nombre = f"{uuid.uuid4().hex}.{extension}"
    os.makedirs(CARPETA_IMG, exist_ok=True)
    archivo.save(os.path.join(CARPETA_IMG, nombre))
    return nombre


def borrar_imagen(nombre):
    if nombre:
        ruta = os.path.join(CARPETA_IMG, nombre)
        if os.path.exists(ruta):
            os.remove(ruta)


def leer_formulario_perfume():
    """Lee y valida el formulario. Devuelve un diccionario o None si hay error."""
    nombre = request.form.get("nombre", "").strip()
    marca = request.form.get("marca", "").strip()
    categoria = request.form.get("categoria", "")
    try:
        precio = float(request.form.get("precio", ""))
        stock = int(request.form.get("stock", ""))
    except ValueError:
        flash("El precio y el stock deben ser números.")
        return None

    if not nombre or categoria not in CATEGORIAS or precio < 0 or stock < 0:
        flash("Revisa los datos: nombre, categoría, precio y stock son obligatorios.")
        return None

    return {"nombre": nombre, "marca": marca, "categoria": categoria,
            "precio": precio, "stock": stock}


# ---------- Protección de rutas ----------
def admin_requerido(f):
    @wraps(f)
    def revisar(*args, **kwargs):
        if session.get("rol") != "admin":
            flash("No tienes permiso para entrar a esa página.")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return revisar

def cliente_requerido(f):
    @wraps(f)
    def revisar(*args, **kwargs):
        if "usuario_id" not in session:
            flash("Inicia sesión para comprar.")
            return redirect(url_for("login"))
        if session.get("rol") == "admin":
            flash("El administrador no puede comprar. Usa una cuenta de cliente.")
            return redirect(url_for("inicio"))
        return f(*args, **kwargs)
    return revisar


# ---------- Rutas del cliente ----------
@app.route("/")
def inicio():
    conn = conectar()
    destacados = conn.execute("""SELECT p.*, COALESCE(SUM(d.cantidad),0) vendidos FROM perfumes p LEFT JOIN detalle_ventas d ON d.perfume_id=p.id GROUP BY p.id ORDER BY vendidos DESC, p.id LIMIT 4""").fetchall()
    conn.close()
    return render_template("index.html", destacados=destacados)

@app.route("/catalogo")
def catalogo():
    categoria = request.args.get("categoria", "todos")
    busqueda = request.args.get("q", "").strip()
    orden = request.args.get("orden", "nombre")

    conn = conectar()
    condiciones, parametros = [], []
    if categoria in ("mujer", "hombre", "unisex"):
        condiciones.append("categoria = ?")
        parametros.append(categoria)
    else:
        categoria = "todos"
    if busqueda:
        condiciones.append("(nombre LIKE ? OR marca LIKE ?)")
        parametros.extend([f"%{busqueda}%", f"%{busqueda}%"])
    orden_sql = {"nombre": "nombre ASC", "precio_asc": "precio ASC", "precio_desc": "precio DESC", "stock": "stock DESC"}.get(orden, "nombre ASC")
    sql = "SELECT * FROM perfumes" + (" WHERE " + " AND ".join(condiciones) if condiciones else "") + " ORDER BY " + orden_sql
    perfumes = conn.execute(sql, parametros).fetchall()
    conn.close()

    return render_template("catalogo.html", perfumes=perfumes, categoria=categoria, busqueda=busqueda, orden=orden)

@app.route("/registro", methods=["GET", "POST"])
def registro():
    if request.method == "POST":
        nombre = request.form["nombre"].strip()
        correo = request.form["correo"].strip().lower()
        contrasena = request.form["contrasena"]

        if not nombre or not correo or len(contrasena) < 6:
            flash("Completa todos los campos. La contraseña debe tener al menos 6 caracteres.")
            return redirect(url_for("registro"))

        conn = conectar()
        try:
            conn.execute(
                "INSERT INTO usuarios (nombre, correo, contrasena, rol) VALUES (?, ?, ?, 'cliente')",
                (nombre, correo, generate_password_hash(contrasena)))
            conn.commit()
        except sqlite3.IntegrityError:
            flash("Ese correo ya está registrado.")
            return redirect(url_for("registro"))
        finally:
            conn.close()

        flash("Cuenta creada. Ya puedes iniciar sesión.")
        return redirect(url_for("login"))

    return render_template("registro.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        correo = request.form["correo"].strip().lower()
        contrasena = request.form["contrasena"]

        conn = conectar()
        usuario = conn.execute("SELECT * FROM usuarios WHERE correo = ?", (correo,)).fetchone()
        conn.close()

        if usuario and check_password_hash(usuario["contrasena"], contrasena):
            session["usuario_id"] = usuario["id"]
            session["nombre"] = usuario["nombre"]
            session["rol"] = usuario["rol"]

            if usuario["rol"] == "admin":
                return redirect(url_for("admin"))
            return redirect(url_for("inicio"))

        flash("Correo o contraseña incorrectos.")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("inicio"))

# ---------- Carrito y compra ----------
@app.route("/carrito")
@cliente_requerido
def carrito():
    items = []
    total = 0
    conn = conectar()
    for pid, cantidad in session.get("carrito", {}).items():
        p = conn.execute("SELECT * FROM perfumes WHERE id = ?", (int(pid),)).fetchone()
        if p:
            subtotal = p["precio"] * cantidad
            items.append({"perfume": p, "cantidad": cantidad, "subtotal": subtotal})
            total += subtotal
    conn.close()
    return render_template("carrito.html", items=items, total=total)


@app.route("/carrito/agregar/<int:id>", methods=["POST"])
@cliente_requerido
def carrito_agregar(id):
    conn = conectar()
    perfume = conn.execute("SELECT * FROM perfumes WHERE id = ?", (id,)).fetchone()
    conn.close()
    if perfume is None:
        abort(404)

    carrito = session.get("carrito", {})
    actual = carrito.get(str(id), 0)

    if actual + 1 > perfume["stock"]:
        flash("No hay más unidades disponibles de ese perfume.")
    else:
        carrito[str(id)] = actual + 1
        session["carrito"] = carrito
        flash(f"{perfume['nombre']} agregado al carrito.")

    return redirect(request.referrer or url_for("catalogo"))


@app.route("/carrito/quitar/<int:id>", methods=["POST"])
@cliente_requerido
def carrito_quitar(id):
    carrito = session.get("carrito", {})
    carrito.pop(str(id), None)
    session["carrito"] = carrito
    return redirect(url_for("carrito"))


ESTADOS_MX = ["Aguascalientes", "Baja California", "Baja California Sur", "Campeche", "Chiapas",
              "Chihuahua", "Ciudad de México", "Coahuila", "Colima", "Durango", "Guanajuato",
              "Guerrero", "Hidalgo", "Jalisco", "México", "Michoacán", "Morelos", "Nayarit",
              "Nuevo León", "Oaxaca", "Puebla", "Querétaro", "Quintana Roo", "San Luis Potosí",
              "Sinaloa", "Sonora", "Tabasco", "Tamaulipas", "Tlaxcala", "Veracruz", "Yucatán",
              "Zacatecas"]
METODOS_PAGO = {"tarjeta": "Tarjeta de crédito o débito",
                "transferencia": "Transferencia bancaria",
                "efectivo": "Pago contra entrega"}


def solo_digitos(texto):
    return "".join(c for c in texto if c.isdigit())


def validar_checkout(f):
    """Revisa los datos del formulario. Devuelve una lista de errores (vacía si todo está bien)."""
    errores = []
    if len(f.get("nombre", "").strip()) < 3:
        errores.append("Escribe tu nombre completo.")
    if len(solo_digitos(f.get("telefono", ""))) != 10:
        errores.append("El teléfono debe tener 10 dígitos.")
    if len(f.get("direccion", "").strip()) < 5:
        errores.append("Escribe tu calle y número.")
    if len(f.get("colonia", "").strip()) < 2:
        errores.append("Escribe tu colonia.")
    if len(f.get("ciudad", "").strip()) < 2:
        errores.append("Escribe tu ciudad o municipio.")
    if f.get("estado") not in ESTADOS_MX:
        errores.append("Elige tu estado.")
    if len(solo_digitos(f.get("cp", ""))) != 5:
        errores.append("El código postal debe tener 5 dígitos.")
    if f.get("metodo_pago") not in METODOS_PAGO:
        errores.append("Elige un método de pago.")

    # Los datos de la tarjeta solo se revisan; no se guardan en la base de datos.
    if f.get("metodo_pago") == "tarjeta":
        if len(f.get("titular", "").strip()) < 3:
            errores.append("Escribe el nombre que aparece en la tarjeta.")
        if not 13 <= len(solo_digitos(f.get("tarjeta", ""))) <= 19:
            errores.append("El número de tarjeta no es válido.")
        vence = f.get("vence", "").strip()
        try:
            mes, anio = vence.split("/")
            mes, anio = int(mes), 2000 + int(anio)
            hoy = datetime.now()
            if not 1 <= mes <= 12 or (anio, mes) < (hoy.year, hoy.month):
                raise ValueError
        except ValueError:
            errores.append("La fecha de vencimiento debe tener el formato MM/AA y no estar vencida.")
        if len(solo_digitos(f.get("cvv", ""))) not in (3, 4):
            errores.append("El CVV debe tener 3 o 4 dígitos.")
    return errores


def lineas_del_carrito(conn):
    """Devuelve (lista de {perfume, cantidad, subtotal}, total) del carrito actual."""
    lineas, total = [], 0
    for pid, cantidad in session.get("carrito", {}).items():
        p = conn.execute("SELECT * FROM perfumes WHERE id = ?", (int(pid),)).fetchone()
        if p:
            subtotal = p["precio"] * cantidad
            lineas.append({"perfume": p, "cantidad": cantidad, "subtotal": subtotal})
            total += subtotal
    return lineas, total


@app.route("/checkout", methods=["GET", "POST"])
@cliente_requerido
def checkout():
    if not session.get("carrito"):
        flash("Tu carrito está vacío.")
        return redirect(url_for("catalogo"))

    conn = conectar()
    items, total = lineas_del_carrito(conn)

    if request.method == "GET":
        conn.close()
        datos = {"nombre": session.get("nombre", ""), "metodo_pago": "tarjeta", "estado": "Tabasco"}
        return render_template("checkout.html", items=items, total=total, datos=datos,
                               estados=ESTADOS_MX, metodos=METODOS_PAGO, errores=[])

    # ---- POST: validar datos y guardar la compra ----
    datos = request.form
    errores = validar_checkout(datos)
    if errores:
        conn.close()
        return render_template("checkout.html", items=items, total=total, datos=datos,
                               estados=ESTADOS_MX, metodos=METODOS_PAGO, errores=errores)

    carrito = session.get("carrito", {})
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1. Revisar stock
        lineas = []
        total = 0
        for pid, cantidad in carrito.items():
            p = conn.execute("SELECT * FROM perfumes WHERE id = ?", (int(pid),)).fetchone()
            if p is None or p["stock"] < cantidad:
                conn.rollback()
                flash("No hay suficiente stock de uno de los perfumes de tu carrito.")
                return redirect(url_for("carrito"))
            lineas.append((p, cantidad))
            total += p["precio"] * cantidad

        # 2. Guardar la venta con los datos de envío (la tarjeta NO se guarda)
        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        direccion = f"{datos['direccion'].strip()}, Col. {datos['colonia'].strip()}"
        cur = conn.execute(
            """INSERT INTO ventas (usuario_id, fecha, total, envio_nombre, envio_telefono,
                                   envio_direccion, envio_ciudad, envio_estado, envio_cp,
                                   envio_referencias, metodo_pago)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session["usuario_id"], fecha, total, datos["nombre"].strip(),
             solo_digitos(datos["telefono"]), direccion, datos["ciudad"].strip(),
             datos["estado"], solo_digitos(datos["cp"]),
             datos.get("referencias", "").strip(), METODOS_PAGO[datos["metodo_pago"]]))
        venta_id = cur.lastrowid

        # 3. Guardar el detalle y descontar el stock
        for p, cantidad in lineas:
            conn.execute(
                """INSERT INTO detalle_ventas (venta_id, perfume_id, cantidad, precio_unitario)
                   VALUES (?, ?, ?, ?)""",
                (venta_id, p["id"], cantidad, p["precio"]))
            conn.execute("UPDATE perfumes SET stock = stock - ? WHERE id = ?",
                         (cantidad, p["id"]))

        conn.commit()
    except Exception:
        conn.rollback()
        flash("Ocurrió un error al procesar la compra. Intenta de nuevo.")
        return redirect(url_for("carrito"))
    finally:
        conn.close()

    session.pop("carrito", None)
    return redirect(url_for("compra_exitosa", id=venta_id))


@app.route("/compra/<int:id>")
@cliente_requerido
def compra_exitosa(id):
    conn = conectar()
    venta = conn.execute("SELECT * FROM ventas WHERE id = ? AND usuario_id = ?",
                         (id, session["usuario_id"])).fetchone()
    if venta is None:
        conn.close()
        abort(404)
    detalle = conn.execute(
        """SELECT p.nombre, d.cantidad, d.precio_unitario
           FROM detalle_ventas d
           JOIN perfumes p ON p.id = d.perfume_id
           WHERE d.venta_id = ?""", (id,)).fetchall()
    conn.close()
    return render_template("compra_exitosa.html", venta=venta, detalle=detalle)


# ---------- Rutas del administrador ----------
@app.route("/admin")
@admin_requerido
def admin():
    conn = conectar()
    resumen = conn.execute("""SELECT (SELECT COUNT(*) FROM ventas) pedidos, (SELECT COALESCE(SUM(total),0) FROM ventas) ingresos, (SELECT COALESCE(SUM(cantidad),0) FROM detalle_ventas) unidades, (SELECT COUNT(*) FROM perfumes WHERE stock <= 5) stock_bajo, (SELECT COUNT(*) FROM usuarios WHERE rol='cliente') clientes""").fetchone()
    dias = conn.execute("SELECT substr(fecha,1,10) dia, ROUND(SUM(total),2) total FROM ventas GROUP BY substr(fecha,1,10) ORDER BY dia DESC LIMIT 14").fetchall()
    populares = conn.execute("""SELECT p.nombre, COALESCE(SUM(d.cantidad),0) unidades FROM perfumes p LEFT JOIN detalle_ventas d ON d.perfume_id=p.id GROUP BY p.id ORDER BY unidades DESC, p.nombre LIMIT 5""").fetchall()
    recientes = conn.execute("SELECT v.*,u.nombre cliente FROM ventas v JOIN usuarios u ON u.id=v.usuario_id ORDER BY v.id DESC LIMIT 5").fetchall()
    conn.close()
    return render_template("admin.html", resumen=resumen, dias=list(reversed([dict(x) for x in dias])), populares=[dict(x) for x in populares], recientes=recientes)


@app.route("/admin/perfumes")
@admin_requerido
def admin_perfumes():
    conn = conectar()
    perfumes = conn.execute("SELECT * FROM perfumes ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin_perfumes.html", perfumes=perfumes)


@app.route("/admin/perfumes/nuevo", methods=["GET", "POST"])
@admin_requerido
def perfume_nuevo():
    if request.method == "POST":
        datos = leer_formulario_perfume()
        if datos is None:
            return redirect(url_for("perfume_nuevo"))

        archivo = request.files.get("imagen")
        imagen = guardar_imagen(archivo)
        if archivo and archivo.filename and imagen is None:
            flash("La foto debe ser JPG, PNG o WEBP.")
            return redirect(url_for("perfume_nuevo"))

        conn = conectar()
        conn.execute(
            """INSERT INTO perfumes (nombre, marca, categoria, precio, stock, imagen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (datos["nombre"], datos["marca"], datos["categoria"],
             datos["precio"], datos["stock"], imagen))
        conn.commit()
        conn.close()
        flash("Perfume agregado correctamente.")
        return redirect(url_for("admin_perfumes"))

    return render_template("admin_perfume_form.html", perfume=None, categorias=CATEGORIAS)


@app.route("/admin/perfumes/<int:id>/editar", methods=["GET", "POST"])
@admin_requerido
def perfume_editar(id):
    conn = conectar()
    perfume = conn.execute("SELECT * FROM perfumes WHERE id = ?", (id,)).fetchone()
    if perfume is None:
        conn.close()
        abort(404)

    if request.method == "POST":
        datos = leer_formulario_perfume()
        if datos is None:
            conn.close()
            return redirect(url_for("perfume_editar", id=id))

        archivo = request.files.get("imagen")
        imagen = perfume["imagen"]
        if archivo and archivo.filename:
            nueva = guardar_imagen(archivo)
            if nueva is None:
                conn.close()
                flash("La foto debe ser JPG, PNG o WEBP.")
                return redirect(url_for("perfume_editar", id=id))
            borrar_imagen(perfume["imagen"])   # quita la foto anterior
            imagen = nueva

        conn.execute(
            """UPDATE perfumes SET nombre=?, marca=?, categoria=?, precio=?, stock=?, imagen=?
               WHERE id=?""",
            (datos["nombre"], datos["marca"], datos["categoria"],
             datos["precio"], datos["stock"], imagen, id))
        conn.commit()
        conn.close()
        flash("Perfume actualizado.")
        return redirect(url_for("admin_perfumes"))

    conn.close()
    return render_template("admin_perfume_form.html", perfume=perfume, categorias=CATEGORIAS)


@app.route("/admin/perfumes/<int:id>/eliminar", methods=["POST"])
@admin_requerido
def perfume_eliminar(id):
    conn = conectar()
    perfume = conn.execute("SELECT * FROM perfumes WHERE id = ?", (id,)).fetchone()
    if perfume is None:
        conn.close()
        abort(404)

    # Si ya se vendió, no se borra para no dañar el historial de ventas
    vendidos = conn.execute(
        "SELECT COUNT(*) FROM detalle_ventas WHERE perfume_id = ?", (id,)).fetchone()[0]
    if vendidos > 0:
        conn.close()
        flash("Este perfume ya tiene ventas registradas, no se puede eliminar. Puedes dejar su stock en 0.")
        return redirect(url_for("admin_perfumes"))

    conn.execute("DELETE FROM perfumes WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    borrar_imagen(perfume["imagen"])
    flash("Perfume eliminado.")
    return redirect(url_for("admin_perfumes"))


@app.route("/mis-pedidos")
@cliente_requerido
def mis_pedidos():
    conn = conectar()
    pedidos = conn.execute("SELECT * FROM ventas WHERE usuario_id=? ORDER BY id DESC", (session["usuario_id"],)).fetchall()
    conn.close()
    return render_template("mis_pedidos.html", pedidos=pedidos)

@app.route("/perfil", methods=["GET", "POST"])
@cliente_requerido
def perfil():
    conn = conectar()
    usuario = conn.execute("SELECT * FROM usuarios WHERE id = ?", (session["usuario_id"],)).fetchone()

    if request.method == "POST":
        accion = request.form.get("accion")

        if accion == "datos":
            nombre = request.form.get("nombre", "").strip()
            if len(nombre) < 3:
                flash("Escribe tu nombre completo.")
            else:
                conn.execute("UPDATE usuarios SET nombre = ? WHERE id = ?", (nombre, usuario["id"]))
                conn.commit()
                session["nombre"] = nombre
                flash("Datos actualizados.")

        elif accion == "contrasena":
            actual = request.form.get("actual", "")
            nueva = request.form.get("nueva", "")
            confirmar = request.form.get("confirmar", "")
            if not check_password_hash(usuario["contrasena"], actual):
                flash("La contraseña actual no es correcta.")
            elif len(nueva) < 6:
                flash("La contraseña nueva debe tener al menos 6 caracteres.")
            elif nueva != confirmar:
                flash("Las contraseñas nuevas no coinciden.")
            else:
                conn.execute("UPDATE usuarios SET contrasena = ? WHERE id = ?",
                             (generate_password_hash(nueva), usuario["id"]))
                conn.commit()
                flash("Contraseña actualizada.")

        conn.close()
        return redirect(url_for("perfil"))

    resumen = conn.execute("SELECT COUNT(*) pedidos FROM ventas WHERE usuario_id = ?",
                           (usuario["id"],)).fetchone()
    ultimo = conn.execute("SELECT * FROM ventas WHERE usuario_id = ? ORDER BY id DESC LIMIT 1",
                          (usuario["id"],)).fetchone()
    conn.close()
    return render_template("perfil.html", usuario=usuario, resumen=resumen, ultimo=ultimo)


@app.route("/admin/usuarios")
@admin_requerido
def admin_usuarios():
    conn = conectar()
    usuarios = conn.execute("""SELECT u.id,u.nombre,u.correo,u.rol,COUNT(v.id) compras,COALESCE(SUM(v.total),0) total FROM usuarios u LEFT JOIN ventas v ON v.usuario_id=u.id GROUP BY u.id ORDER BY u.id DESC""").fetchall()
    conn.close()
    return render_template("admin_usuarios.html", usuarios=usuarios)

@app.route("/admin/pedidos")
@admin_requerido
def admin_pedidos():
    conn = conectar()
    pedidos = conn.execute("SELECT v.*,u.nombre cliente,u.correo FROM ventas v JOIN usuarios u ON u.id=v.usuario_id ORDER BY v.id DESC").fetchall()
    conn.close()
    return render_template("admin_pedidos.html", pedidos=pedidos)

@app.route("/admin/pedidos/<int:id>/estado", methods=["POST"])
@admin_requerido
def actualizar_estado(id):
    estado = request.form.get("estado")
    if estado not in ("Recibido", "Preparando", "En camino", "Entregado"):
        abort(400)
    conn = conectar()
    conn.execute("UPDATE ventas SET estado=? WHERE id=?", (estado,id))
    conn.commit()
    conn.close()
    flash("Estado actualizado")
    return redirect(url_for("admin_pedidos"))

def normalizar(t):
    import re
    t = ''.join(c for c in unicodedata.normalize('NFD', str(t).lower()) if unicodedata.category(c) != 'Mn')
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def responder_consulta(conn, pregunta):
    """Consultas de solo lectura; reconoce lenguaje coloquial sin ejecutar SQL del usuario."""
    from difflib import get_close_matches
    q = normalizar(pregunta)
    if not q:
        return "Escribe tu pregunta sobre ventas, pedidos, clientes o perfumes."
    words = q.split()
    aliases = {
        'vendimos':'ventas','vendio':'ventas','vendieron':'ventas','vendi':'ventas',
        'facturamos':'ingresos','facturado':'ingresos','recaudado':'ingresos',
        'ganamos':'ganancias','ganancia':'ganancias','plata':'dinero',
        'compras':'ventas','compraron':'ventas','comprado':'vendido',
        'ordenes':'pedidos','orden':'pedidos','encargos':'pedidos',
        'existencias':'stock','existencia':'stock','quedan':'stock',
        'disponibles':'stock','disponible':'stock','agotados':'agotado',
        'escasos':'bajo','faltan':'bajo','pocos':'bajo',
        'usuarios':'clientes','compradores':'clientes',
        'articulos':'productos','fragancias':'perfumes',
        'ayer':'ayer','ahorita':'hoy','actualmente':'hoy',
    }
    vocabulary = set(aliases) | set(aliases.values()) | set('hoy ayer mes semana ano total mas menos mejor peor vendido popular top ranking stock inventario bajo agotado clientes pedidos estado envio entrega pendientes perfumes productos catalogo precio caro barato marca categoria hombre mujer unisex cuantos cuantas cuanto cual dime muestra lista hay tenemos dinero ingresos ventas ganancias nombre ultimos recientes registrado'.split())
    tokens = [aliases.get(w, w) for w in words]
    tokens = [get_close_matches(w, vocabulary, n=1, cutoff=.83)[0] if w not in vocabulary and get_close_matches(w, vocabulary, n=1, cutoff=.83) else w for w in tokens]
    q = ' '.join(tokens)
    def has(*terms): return any(t in tokens or t in q for t in terms)
    def money(n): return f"${n:,.2f} MXN"
    # Nombres/marcas: detectar menciones concretas antes de las categorías genéricas.
    productos = conn.execute('SELECT id,nombre,marca,categoria,precio,stock FROM perfumes ORDER BY nombre').fetchall()
    matches = [p for p in productos if (normalizar(p['nombre']) in q or (p['marca'] and normalizar(p['marca']) in q))]
    if matches and not has('mas vendido','top','ranking','ventas totales'):
        p = matches[0]
        d = conn.execute('SELECT COALESCE(SUM(cantidad),0) n, COALESCE(SUM(cantidad*precio_unitario),0) importe FROM detalle_ventas WHERE perfume_id=?',(p['id'],)).fetchone()
        return f"{p['nombre']} ({p['marca'] or 'sin marca'}): precio {money(p['precio'])}, stock {p['stock']} unidades, categoría {p['categoria']}; se vendieron {d['n']} unidades por {money(d['importe'])}."
    if has('mas vendido','mas popular','top','ranking','mejor vendido','menos vendido','mas comprado') or (has('vendido') and has('cual','que','perfume','producto')):
        ascending = has('menos','peor')
        xs = conn.execute('SELECT p.nombre,SUM(d.cantidad) unidades,SUM(d.cantidad*d.precio_unitario) ingresos FROM detalle_ventas d JOIN perfumes p ON p.id=d.perfume_id GROUP BY p.id ORDER BY unidades '+('ASC' if ascending else 'DESC')+' LIMIT 5').fetchall()
        if not xs: return 'Todavía no hay ventas registradas para calcular un ranking.'
        if has('top','ranking','lista','muestra'): return 'Ranking de perfumes: ' + '; '.join(f"{x['nombre']}: {x['unidades']} unidades ({money(x['ingresos'])})" for x in xs) + '.'
        x=xs[0];return f"El perfume {'menos' if ascending else 'más'} vendido es {x['nombre']}, con {x['unidades']} unidades y {money(x['ingresos'])} en ingresos."
    if has('precio','caro','barato','cuesta','costos'):
        if not productos:return 'No hay perfumes registrados.'
        order=sorted(productos,key=lambda p:p['precio'],reverse=not has('barato','economico','menos'))
        return 'Precios del catálogo: '+ '; '.join(f"{p['nombre']}: {money(p['precio'])}" for p in order[:8])+'.'
    if has('stock','inventario','agotado','reponer','bajo','existencias'):
        if has('agotado','cero','sin stock'):
            xs=[p for p in productos if p['stock']==0]; label='agotados'
        elif has('bajo','reponer','poco','escaso','faltan'):
            xs=[p for p in productos if p['stock']<=5];label='con 5 unidades o menos'
        else:
            xs=productos;label='en inventario'
        if not xs:return f'No hay perfumes {label}.'
        return f"Perfumes {label}: " + '; '.join(f"{p['nombre']} ({p['stock']} unidades)" for p in xs[:15]) + (f'. Total: {sum(p["stock"] for p in xs)} unidades.' )
    if has('clientes','cliente','usuarios','usuario'):
        x=conn.execute("SELECT COUNT(*) n FROM usuarios WHERE rol='cliente'").fetchone()
        return f"Hay {x['n']} clientes registrados."
    if has('pedidos','pedido','envio','entrega','estado','pendientes','ordenes'):
        xs=conn.execute('SELECT estado,COUNT(*) n FROM ventas GROUP BY estado ORDER BY n DESC').fetchall()
        if not xs:return 'Todavía no hay pedidos registrados.'
        return 'Pedidos por estado: '+', '.join(f"{x['estado']}: {x['n']}" for x in xs)+'.'
    if has('perfumes','productos','catalogo','marca','categoria','hombre','mujer','unisex') and not has('ventas','ingresos','dinero','ganancias'):
        xs=[p for p in productos if not has('hombre','mujer','unisex') or p['categoria'] in [w for w in ('hombre','mujer','unisex') if w in tokens]]
        if has('lista','muestra','cual','que','hombre','mujer','unisex','marca'):
            return 'Perfumes disponibles: '+('; '.join(f"{p['nombre']} ({p['marca']}, {p['stock']} disponibles)" for p in xs[:15]) if xs else 'ninguno')+'.'
        return f"El catálogo tiene {len(productos)} perfumes y {sum(p['stock'] for p in productos)} unidades en inventario."
    if has('ventas','ingresos','ganancias','dinero','total','recaudado','facturacion','hoy','ayer','mes','semana','ano'):
        where=''; label='En total'
        if has('hoy'):
            where=" WHERE substr(fecha,1,10)=date('now','localtime')";label='Hoy'
        elif has('ayer'):
            where=" WHERE substr(fecha,1,10)=date('now','-1 day','localtime')";label='Ayer'
        elif has('semana'):
            where=" WHERE date(substr(fecha,1,10))>=date('now','-6 days','localtime')";label='En los últimos 7 días'
        elif has('mes','mensual'):
            where=" WHERE substr(fecha,1,7)=strftime('%Y-%m','now','localtime')";label='Este mes'
        elif has('ano','anual'):
            where=" WHERE substr(fecha,1,4)=strftime('%Y','now','localtime')";label='Este año'
        x=conn.execute('SELECT COUNT(*) n,COALESCE(SUM(total),0) total FROM ventas'+where).fetchone()
        return f"{label} se registraron {x['n']} compras por {money(x['total'])} de ingresos brutos. La base de datos no contiene costos para calcular ganancias netas."
    return 'Puedo responder con los datos de la tienda: ventas por fecha, ingresos, perfumes y precios, existencias, productos agotados, más vendidos, clientes y estados de pedidos. Por ejemplo: «¿qué se vendió más?», «¿cuánta plata entró ayer?» o «¿cuánto stock queda de Dior?». Si tu pregunta es sobre otro tema, necesito que la relaciones con los datos registrados.'


@app.route("/admin/asistente", methods=["POST"])
@admin_requerido
def asistente():
    data = request.get_json(silent=True) or {}
    pregunta = str(data.get('pregunta', ''))[:300]
    conn = conectar()
    try:
        respuesta = responder_consulta(conn, pregunta)
    finally:
        conn.close()
    return jsonify({'respuesta':respuesta})

crear_tablas()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")