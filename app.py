import os
import uuid
import sqlite3
import secrets
import unicodedata
from flask import jsonify
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, abort, send_file)
from werkzeug.security import generate_password_hash, check_password_hash
from database import conectar, crear_tablas
from datetime import datetime, timedelta, date
import io
import smtplib
import threading
from email.message import EmailMessage

app = Flask(__name__)
app.secret_key = os.environ.get("FRAG_SECRET_KEY", secrets.token_hex(32))

app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024   # fotos de máximo 5 MB

CARPETA_IMG = os.path.join(app.root_path, "static", "img")
EXTENSIONES = {"jpg", "jpeg", "png", "webp"}

# Los comprobantes de pago se guardan FUERA de "static" para que nadie pueda abrirlos por URL:
# solo los ve el cliente dueño del pedido y el administrador (ruta /comprobante/<id>).
CARPETA_COMPROBANTES = os.path.join(app.root_path, "comprobantes")
EXT_COMPROBANTE = {"pdf", "jpg", "jpeg", "png", "webp"}
ESTADOS_PAGADOS = ("En proceso", "En camino", "Entregado", "Recibido")
ESTADOS_SIN_PAGO = ("Pendiente de pago", "Comprobante enviado", "Pago rechazado")
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


def _tipo_real(cabecera):
    """Devuelve la extensión real según los primeros bytes del archivo (no confía en el nombre)."""
    if cabecera.startswith(b"%PDF"):
        return "pdf"
    if cabecera.startswith(b"\x89PNG"):
        return "png"
    if cabecera.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if cabecera[:4] == b"RIFF" and cabecera[8:12] == b"WEBP":
        return "webp"
    return None


def guardar_comprobante(archivo):
    """Guarda el comprobante (PDF o imagen) y devuelve su nombre. None si el archivo no es válido."""
    if not archivo or archivo.filename == "" or "." not in archivo.filename:
        return None
    if archivo.filename.rsplit(".", 1)[1].lower() not in EXT_COMPROBANTE:
        return None
    tipo = _tipo_real(archivo.read(12))
    archivo.seek(0)
    if tipo is None:
        return None
    nombre = f"{uuid.uuid4().hex}.{tipo}"
    os.makedirs(CARPETA_COMPROBANTES, exist_ok=True)
    archivo.save(os.path.join(CARPETA_COMPROBANTES, nombre))
    return nombre


def borrar_comprobante(nombre):
    if nombre:
        ruta = os.path.join(CARPETA_COMPROBANTES, os.path.basename(nombre))
        if os.path.exists(ruta):
            os.remove(ruta)


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
    destacados = conn.execute("""SELECT p.*, COALESCE(SUM(d.cantidad),0) vendidos FROM perfumes p LEFT JOIN detalle_pagado d ON d.perfume_id=p.id GROUP BY p.id ORDER BY vendidos DESC, p.id LIMIT 4""").fetchall()
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


def emitir_codigo(usuario, finalidad):
    import time, hashlib, hmac
    ahora = int(time.time())
    conn = conectar()
    conn.execute("CREATE TABLE IF NOT EXISTS codigos_correo (uid INTEGER, finalidad TEXT, digest TEXT, vence INTEGER, intentos INTEGER, creado INTEGER, PRIMARY KEY(uid, finalidad))")
    previo = conn.execute("SELECT creado FROM codigos_correo WHERE uid=? AND finalidad=?", (usuario['id'], finalidad)).fetchone()
    if previo and ahora - previo['creado'] < 60:
        conn.close()
        return False
    codigo = f"{secrets.randbelow(1000000):06d}"
    digest = hmac.new(app.secret_key.encode(), codigo.encode(), hashlib.sha256).hexdigest()
    conn.execute("INSERT OR REPLACE INTO codigos_correo VALUES (?, ?, ?, ?, 0, ?)", (usuario['id'], finalidad, digest, ahora+300, ahora))
    conn.commit(); conn.close()
    if not _enviar_correo(usuario['correo'], 'Código de seguridad FRAG-IA', f'Tu código para {finalidad} es {codigo}. Caduca en 5 minutos. No lo compartas.'):
        return False
    session['verificacion'] = {'uid': usuario['id'], 'finalidad': finalidad}
    return True

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        conn = conectar()
        usuario = conn.execute('SELECT * FROM usuarios WHERE correo=?', (request.form['correo'].strip().lower(),)).fetchone()
        conn.close()
        if usuario and check_password_hash(usuario['contrasena'], request.form['contrasena']):
            session.clear()
            if emitir_codigo(usuario, 'iniciar sesión'):
                return redirect(url_for('verificar_correo'))
            flash('No se pudo enviar el código. Revisa la configuración de Gmail o espera un minuto.')
        else:
            flash('Correo o contraseña incorrectos.')
    return render_template('login.html')

@app.route('/recuperar', methods=['GET', 'POST'])
def recuperar():
    if request.method == 'POST':
        conn = conectar()
        usuario = conn.execute('SELECT * FROM usuarios WHERE correo=?', (request.form['correo'].strip().lower(),)).fetchone()
        conn.close()
        session.pop('verificacion', None)
        if usuario:
            emitir_codigo(usuario, 'cambiar contraseña')
        flash('Si la cuenta existe y el correo está configurado, recibirás un código. Revisa tu bandeja y spam.')
        return redirect(url_for('verificar_correo'))
    return render_template('recuperar.html')

@app.route('/verificar-correo', methods=['GET', 'POST'])
def verificar_correo():
    import time, hashlib, hmac
    pendiente = session.get('verificacion')
    cambio = bool(pendiente and pendiente['finalidad'] == 'cambiar contraseña')
    if request.method == 'POST':
        if not pendiente:
            flash('Solicita un código nuevo.')
            return redirect(url_for('recuperar'))
        nueva = request.form.get('nueva', '')
        if cambio and (len(nueva)<6 or nueva != request.form.get('confirmar')):
            flash('Las contraseñas deben coincidir y tener al menos 6 caracteres.')
            return redirect(url_for('verificar_correo'))
        conn = conectar(); conn.execute('BEGIN IMMEDIATE')
        uid, finalidad = pendiente['uid'], pendiente['finalidad']
        fila = conn.execute('SELECT * FROM codigos_correo WHERE uid=? AND finalidad=?', (uid, finalidad)).fetchone()
        digest = hmac.new(app.secret_key.encode(), request.form.get('codigo','').strip().encode(), hashlib.sha256).hexdigest()
        if not fila or fila['vence'] < time.time() or fila['intentos'] >= 5 or not hmac.compare_digest(fila['digest'], digest):
            if fila: conn.execute('UPDATE codigos_correo SET intentos=intentos+1 WHERE uid=? AND finalidad=?', (uid, finalidad))
            conn.commit(); conn.close()
            flash('Código incorrecto, vencido o sin intentos disponibles. Solicita otro si es necesario.')
            return redirect(url_for('verificar_correo'))
        usuario = conn.execute('SELECT * FROM usuarios WHERE id=?', (uid,)).fetchone()
        conn.execute('DELETE FROM codigos_correo WHERE uid=? AND finalidad=?', (uid, finalidad))
        if cambio:
            conn.execute('UPDATE usuarios SET contrasena=? WHERE id=?', (generate_password_hash(nueva), uid))
            conn.execute('DELETE FROM codigos_correo WHERE uid=?', (uid,))
        conn.commit(); conn.close(); session.clear()
        if cambio:
            avisar_cliente(usuario['correo'], 'Contraseña actualizada FRAG-IA', 'Tu contraseña fue actualizada. Si no reconoces este cambio, contacta con la tienda.')
            flash('Contraseña actualizada. Inicia sesión nuevamente.')
            return redirect(url_for('login'))
        session.update(usuario_id=uid, nombre=usuario['nombre'], rol=usuario['rol'])
        return redirect(url_for('admin' if usuario['rol']=='admin' else 'inicio'))
    return render_template('verificar_correo.html', cambio=cambio)


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


@app.route("/carrito/cantidad/<int:id>", methods=["POST"])
@cliente_requerido
def carrito_cantidad(id):
    """Sube o baja de uno en uno la cantidad de un perfume desde el carrito."""
    carrito = session.get("carrito", {})
    actual = carrito.get(str(id))
    if actual is None:
        return redirect(url_for("carrito"))

    accion = request.form.get("accion")
    if accion == "mas":
        conn = conectar()
        perfume = conn.execute("SELECT stock FROM perfumes WHERE id = ?", (id,)).fetchone()
        conn.close()
        if perfume is None or actual + 1 > perfume["stock"]:
            flash("No hay más unidades disponibles de ese perfume.")
        else:
            carrito[str(id)] = actual + 1
    elif accion == "menos" and actual > 1:
        carrito[str(id)] = actual - 1

    session["carrito"] = carrito
    return redirect(url_for("carrito"))


ESTADOS_MX = ["Aguascalientes", "Baja California", "Baja California Sur", "Campeche", "Chiapas",
              "Chihuahua", "Ciudad de México", "Coahuila", "Colima", "Durango", "Guanajuato",
              "Guerrero", "Hidalgo", "Jalisco", "México", "Michoacán", "Morelos", "Nayarit",
              "Nuevo León", "Oaxaca", "Puebla", "Querétaro", "Quintana Roo", "San Luis Potosí",
              "Sinaloa", "Sonora", "Tabasco", "Tamaulipas", "Tlaxcala", "Veracruz", "Yucatán",
              "Zacatecas"]
# Único método de pago: transferencia bancaria con comprobante.
METODOS_PAGO = {"transferencia": "Transferencia bancaria"}

# ---------- Cuenta del administrador para transferencias ----------
# DATOS DE EJEMPLO: cámbialos por los reales.
CUENTA_BANCARIA = {
    "banco": "BBVA",
    "titular": "FRAG-IA Perfumería",
    "cuenta": "0123456789",
    "clabe": "012345678901234567",
}

# ---------- Correo de avisos (se configura en config_correo.py) ----------
try:
    from config_correo import CORREO_EMISOR, CORREO_CLAVE
except ImportError:
    CORREO_EMISOR, CORREO_CLAVE = "", ""


def _enviar_correo(destino, asunto, texto):
    """Envía el correo. Si falla, solo avisa en la consola: la compra nunca se cancela."""
    if not CORREO_EMISOR or not CORREO_CLAVE or "@" not in (destino or ""):
        print("Aviso: correo no enviado (falta configurar config_correo.py).")
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = asunto
        msg["From"] = f"FRAG-IA Perfumería <{CORREO_EMISOR}>"
        msg["To"] = destino
        msg.set_content(texto)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(CORREO_EMISOR, CORREO_CLAVE)
            smtp.send_message(msg)
        return True
    except Exception as e:
        print("No se pudo enviar el correo:", e)
        return False


def avisar_cliente(destino, asunto, texto):
    """Manda un correo en segundo plano (si el correo no está configurado, solo avisa en consola)."""
    threading.Thread(target=_enviar_correo, args=(destino, asunto, texto), daemon=True).start()


def avisar_compra(destino, venta_id, fecha, nombre, lineas, total, metodo_clave, envio):
    """Correo al registrar el pedido: ya incluye el comprobante, queda en revisión."""
    detalle = "\n".join(f"  - {p['nombre']} x{c}  ${p['precio'] * c:,.2f}" for p, c in lineas)
    texto = (f"Hola {nombre},\n\n"
             f"¡Gracias por tu compra en FRAG-IA! Tu pedido #{venta_id} se registró el {fecha}.\n\n"
             f"Productos:\n{detalle}\n\n"
             f"Total: ${total:,.2f}\n"
             "Método de pago: transferencia bancaria\n\n"
             "Recibimos tu comprobante y lo estamos revisando. Te avisaremos por correo cuando el pago "
             "quede confirmado y empecemos a preparar tu envío.\n\n"
             f"Enviaremos tu pedido a:\n  {envio}\n\n"
             "Puedes ver el estado de tu pedido en Mis pedidos.\n\nFRAG-IA Perfumería")
    avisar_cliente(destino, f"Recibimos tu pedido #{venta_id} en FRAG-IA", texto)


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
        u = conn.execute("SELECT * FROM usuarios WHERE id = ?", (session["usuario_id"],)).fetchone()
        conn.close()
        # Si el cliente ya compró antes (o guardó su dirección en su perfil), sus datos aparecen llenos
        datos = {"nombre": session.get("nombre", ""),
                 "telefono": u["dir_telefono"] or "",
                 "direccion": u["dir_calle"] or "",
                 "colonia": u["dir_colonia"] or "",
                 "ciudad": u["dir_ciudad"] or "",
                 "estado": u["dir_estado"] or "Tabasco",
                 "cp": u["dir_cp"] or "",
                 "referencias": u["dir_referencias"] or "",
                 "metodo_pago": "transferencia"}
        return render_template("checkout.html", items=items, total=total, datos=datos,
                               estados=ESTADOS_MX, metodos=METODOS_PAGO, errores=[],
                               cuenta=CUENTA_BANCARIA)

    # ---- POST: validar datos + comprobante y guardar el pedido ----
    datos = request.form
    errores = validar_checkout(datos)
    nombre_comprobante = None
    archivo = request.files.get("comprobante")
    if archivo is None or archivo.filename == "":
        errores.append("Sube el comprobante de tu transferencia para poder confirmar el pedido.")
    elif not errores:
        nombre_comprobante = guardar_comprobante(archivo)
        if nombre_comprobante is None:
            errores.append("El comprobante debe ser PDF, JPG, PNG o WEBP (máximo 5 MB).")
    if errores:
        conn.close()
        return render_template("checkout.html", items=items, total=total, datos=datos,
                               estados=ESTADOS_MX, metodos=METODOS_PAGO, errores=errores,
                               cuenta=CUENTA_BANCARIA)

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
                borrar_comprobante(nombre_comprobante)
                flash("No hay suficiente stock de uno de los perfumes de tu carrito.")
                return redirect(url_for("carrito"))
            lineas.append((p, cantidad))
            total += p["precio"] * cantidad

        # 2. Guardar el pedido con su comprobante: queda "Comprobante enviado" hasta que el admin
        #    verifique la transferencia. El stock se aparta desde ahora y se devuelve si se cancela.
        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        direccion = f"{datos['direccion'].strip()}, Col. {datos['colonia'].strip()}"
        cur = conn.execute(
            """INSERT INTO ventas (usuario_id, fecha, total, estado, envio_nombre, envio_telefono,
                                   envio_direccion, envio_ciudad, envio_estado, envio_cp,
                                   envio_referencias, metodo_pago, comprobante, comprobante_fecha)
               VALUES (?, ?, ?, 'Comprobante enviado', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session["usuario_id"], fecha, total, datos["nombre"].strip(),
             solo_digitos(datos["telefono"]), direccion, datos["ciudad"].strip(),
             datos["estado"], solo_digitos(datos["cp"]),
             datos.get("referencias", "").strip(), METODOS_PAGO["transferencia"],
             nombre_comprobante, fecha))
        venta_id = cur.lastrowid

        # Guardar la dirección en la cuenta del cliente para sus próximas compras
        conn.execute(
            """UPDATE usuarios SET dir_telefono = ?, dir_calle = ?, dir_colonia = ?, dir_ciudad = ?,
                                   dir_estado = ?, dir_cp = ?, dir_referencias = ?
               WHERE id = ?""",
            (solo_digitos(datos["telefono"]), datos["direccion"].strip(), datos["colonia"].strip(),
             datos["ciudad"].strip(), datos["estado"], solo_digitos(datos["cp"]),
             datos.get("referencias", "").strip(), session["usuario_id"]))

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
        borrar_comprobante(nombre_comprobante)
        flash("Ocurrió un error al procesar la compra. Intenta de nuevo.")
        return redirect(url_for("carrito"))
    finally:
        conn.close()

    # Aviso por correo al cliente (si falla, la compra ya quedó guardada)
    try:
        c2 = conectar()
        u = c2.execute("SELECT correo FROM usuarios WHERE id = ?", (session["usuario_id"],)).fetchone()
        c2.close()
        envio = (f"{direccion}, {datos['ciudad'].strip()}, {datos['estado']}. "
                 f"C.P. {solo_digitos(datos['cp'])}")
        avisar_compra(u["correo"], venta_id, fecha, datos["nombre"].strip(), lineas, total,
                      "transferencia", envio)
    except Exception as e:
        print("No se pudo preparar el aviso por correo:", e)

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
    return render_template("compra_exitosa.html", venta=venta, detalle=detalle,
                           cuenta=CUENTA_BANCARIA)


# ---------- Rutas del administrador ----------
@app.route("/admin")
@admin_requerido
def admin():
    conn = conectar()
    resumen = conn.execute("""SELECT (SELECT COUNT(*) FROM ventas_pagadas) pedidos, (SELECT COALESCE(SUM(total),0) FROM ventas_pagadas) ingresos, (SELECT COALESCE(SUM(cantidad),0) FROM detalle_pagado) unidades, (SELECT COUNT(*) FROM ventas WHERE estado = 'Comprobante enviado') por_revisar, (SELECT COUNT(*) FROM perfumes WHERE stock <= 5) stock_bajo, (SELECT COUNT(*) FROM usuarios WHERE rol='cliente') clientes""").fetchone()
    dias = conn.execute("SELECT substr(fecha,1,10) dia, ROUND(SUM(total),2) total FROM ventas_pagadas GROUP BY substr(fecha,1,10) ORDER BY dia DESC LIMIT 14").fetchall()
    populares = conn.execute("""SELECT p.nombre, COALESCE(SUM(d.cantidad),0) unidades FROM perfumes p LEFT JOIN detalle_pagado d ON d.perfume_id=p.id GROUP BY p.id ORDER BY unidades DESC, p.nombre LIMIT 5""").fetchall()
    recientes = conn.execute("SELECT v.*,u.nombre cliente FROM ventas v JOIN usuarios u ON u.id=v.usuario_id ORDER BY v.id DESC LIMIT 5").fetchall()
    conn.close()
    return render_template("admin.html", resumen=resumen, dias=list(reversed([dict(x) for x in dias])), populares=[dict(x) for x in populares], recientes=recientes)


# ---------- Reportes de ventas por periodo ----------
MESES_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


# ---------- Inventario, costos y ganancia (método PEPS) ----------
def asegurar_costos():
    """Crea la tabla de entradas de inventario (se llama "compras") y la columna de costo (si no existen). No borra nada."""
    conn = conectar()
    conn.execute("""CREATE TABLE IF NOT EXISTS compras (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        perfume_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        cantidad INTEGER NOT NULL,
        costo_unitario REAL NOT NULL,
        proveedor TEXT,
        FOREIGN KEY (perfume_id) REFERENCES perfumes(id)
    )""")
    conn.execute("CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT NOT NULL)")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(perfumes)").fetchall()]
    if "costo" not in cols:
        conn.execute("ALTER TABLE perfumes ADD COLUMN costo REAL NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


COSTO_ESTIMADO_POR_DEFECTO = 65.0


def _porcentaje_estimado(conn):
    """% del precio de venta que se supone como costo cuando no hay costo real (0 = desactivado)."""
    r = conn.execute("SELECT valor FROM ajustes WHERE clave = 'costo_estimado_pct'").fetchone()
    try:
        v = float(r["valor"]) if r else COSTO_ESTIMADO_POR_DEFECTO
    except ValueError:
        v = COSTO_ESTIMADO_POR_DEFECTO
    return min(max(v, 0.0), 100.0)


def _peps(conn):
    """Método PEPS (primeras entradas, primeras salidas).
    Cada venta se descuenta de la entrada de inventario más antigua que todavía tenga piezas y su costo es
    el que se pagó por esa entrada. Si una pieza no tiene costo real, se estima como un % de su precio
    de venta (ver _porcentaje_estimado). Devuelve (lineas_vendidas, lotes_restantes, perfumes)."""
    pct = _porcentaje_estimado(conn) / 100
    perfumes = {r["id"]: r for r in conn.execute("SELECT id, nombre, marca, stock, costo, precio FROM perfumes")}
    compras = conn.execute("""SELECT id, perfume_id, cantidad, costo_unitario
                              FROM compras ORDER BY fecha, id""").fetchall()
    ventas = conn.execute("""SELECT d.id, d.perfume_id, d.cantidad, d.precio_unitario, v.fecha
                             FROM detalle_ventas d JOIN ventas_pagadas v ON v.id = d.venta_id
                             ORDER BY v.fecha, v.id, d.id""").fetchall()
    vendidas, recibidas, apartadas = {}, {}, {}
    for r in conn.execute("""SELECT d.perfume_id, SUM(d.cantidad) n FROM detalle_ventas d
                             JOIN ventas v ON v.id = d.venta_id
                             WHERE v.estado IN ('Pendiente de pago','Comprobante enviado','Pago rechazado')
                             GROUP BY d.perfume_id"""):
        apartadas[r["perfume_id"]] = r["n"]
    for v in ventas:
        vendidas[v["perfume_id"]] = vendidas.get(v["perfume_id"], 0) + v["cantidad"]
    for c in compras:
        recibidas[c["perfume_id"]] = recibidas.get(c["perfume_id"], 0) + c["cantidad"]

    # Lote 0 = lo que ya había antes de registrar entradas (usa el "costo" del perfume)
    lotes = {}
    for pid, pf in perfumes.items():
        inicial = pf["stock"] + vendidas.get(pid, 0) + apartadas.get(pid, 0) - recibidas.get(pid, 0)
        lotes[pid] = [[inicial, pf["costo"], "Inventario inicial", None]] if inicial > 0 else []
    for c in compras:
        lotes.setdefault(c["perfume_id"], []).append(
            [c["cantidad"], c["costo_unitario"], f"Entrada #{c['id']}", c["id"]])

    lineas = []
    for v in ventas:
        pid, falta, costo, sin_costo, estimado = v["perfume_id"], v["cantidad"], 0.0, False, False
        consumos = []                                   # (origen, orden, piezas, costo c/u, estimado)
        cola = lotes.get(pid, [])
        while falta > 0 and cola:
            lote = cola[0]
            tomar = min(falta, lote[0])
            costo_u = lote[1]
            est_lote = False
            if costo_u <= 0:                       # sin costo real: se estima (o se avisa si está desactivado)
                costo_u = v["precio_unitario"] * pct
                if pct > 0:
                    estimado = est_lote = True
                else:
                    sin_costo = True
            costo += tomar * costo_u
            consumos.append((lote[2], lote[3] or 0, tomar, costo_u, est_lote))
            lote[0] -= tomar
            falta -= tomar
            if lote[0] == 0:
                cola.pop(0)
        if falta > 0:  # se vendió más de lo registrado: se usa el costo base del perfume
            base = perfumes[pid]["costo"] if pid in perfumes else 0
            est_base = False
            if base <= 0:
                base = v["precio_unitario"] * pct
                if pct > 0:
                    estimado = est_base = True
                else:
                    sin_costo = True
            costo += falta * base
            consumos.append(("Sin entrada registrada", 10**9, falta, base, est_base))
        lineas.append({"perfume_id": pid, "fecha": v["fecha"][:10], "cantidad": v["cantidad"],
                       "ingreso": v["cantidad"] * v["precio_unitario"],
                       "costo": costo, "sin_costo": sin_costo, "estimado": estimado, "consumos": consumos})

    # Valor del inventario restante: las piezas sin costo real se estiman con el precio actual
    for pid, ls in lotes.items():
        for lote in ls:
            if lote[1] <= 0 and pct > 0 and pid in perfumes:
                lote[1] = perfumes[pid]["precio"] * pct
                lote.append(True)
            else:
                lote.append(False)
    return lineas, lotes, perfumes


@app.route("/admin/inventario")
@admin_requerido
def admin_inventario():
    conn = conectar()
    perfumes = conn.execute("SELECT id, nombre, marca, stock, costo, precio FROM perfumes ORDER BY nombre").fetchall()
    compras = conn.execute("""SELECT c.id, c.fecha, c.cantidad, c.costo_unitario, c.proveedor, p.nombre
                              FROM compras c JOIN perfumes p ON p.id = c.perfume_id
                              ORDER BY c.fecha DESC, c.id DESC""").fetchall()
    _, lotes, _ = _peps(conn)
    pct_estimado = _porcentaje_estimado(conn)
    conn.close()
    inventario = []
    for pf in perfumes:
        ls = [{"piezas": q, "costo": c, "origen": o, "estimado": est}
              for q, c, o, _, est in lotes.get(pf["id"], []) if q > 0]
        inventario.append({"nombre": pf["nombre"], "lotes": ls,
                           "piezas": sum(l["piezas"] for l in ls),
                           "valor": sum(l["piezas"] * l["costo"] for l in ls)})
    restante = {cid: q for ls in lotes.values() for q, c, o, cid, est in ls if cid is not None}
    return render_template("admin_inventario.html", perfumes=perfumes, compras=compras,
                           inventario=inventario, restante=restante,
                           total_inventario=sum(i["valor"] for i in inventario),
                           pct_estimado=pct_estimado, hoy=date.today().isoformat())


@app.route("/admin/inventario/entrada", methods=["POST"])
@admin_requerido
def entrada_nueva():
    try:
        perfume_id = int(request.form["perfume_id"])
        cantidad = int(request.form["cantidad"])
        fecha = date.fromisoformat(request.form.get("fecha") or date.today().isoformat())
    except (ValueError, KeyError):
        flash("Revisa los datos de las piezas que quieres agregar.")
        return redirect(url_for("admin_inventario"))
    conn = conectar()
    perfume = conn.execute("SELECT id, precio FROM perfumes WHERE id = ?", (perfume_id,)).fetchone()
    if perfume is None:
        conn.close()
        flash("Ese perfume no existe.")
        return redirect(url_for("admin_inventario"))
    # El costo se escribe en pesos o se calcula con un % del precio de venta (se guarda en pesos)
    costo_txt = request.form.get("costo", "").replace(",", ".").strip()
    pct_txt = request.form.get("pct", "").replace(",", ".").strip()
    try:
        if costo_txt:
            costo = float(costo_txt)
        elif pct_txt:
            costo = round(perfume["precio"] * float(pct_txt) / 100, 2)
        else:
            raise ValueError
    except ValueError:
        conn.close()
        flash("Escribe el costo por pieza o el porcentaje del precio de venta.")
        return redirect(url_for("admin_inventario"))
    if cantidad < 1 or costo <= 0:
        conn.close()
        flash("Las piezas deben ser al menos 1 y el costo debe ser mayor a 0.")
        return redirect(url_for("admin_inventario"))
    proveedor = request.form.get("proveedor", "").strip()[:80]
    conn.execute("""INSERT INTO compras (perfume_id, fecha, cantidad, costo_unitario, proveedor)
                    VALUES (?, ?, ?, ?, ?)""", (perfume_id, fecha.isoformat(), cantidad, costo, proveedor))
    conn.execute("UPDATE perfumes SET stock = stock + ? WHERE id = ?", (cantidad, perfume_id))
    conn.commit()
    conn.close()
    flash(f"Piezas agregadas al inventario: {cantidad} pieza(s) a ${costo:,.2f} cada una.")
    return redirect(url_for("admin_inventario"))


@app.route("/admin/inventario/costos", methods=["POST"])
@admin_requerido
def inventario_costos():
    conn = conectar()
    cambios = 0
    # El porcentaje general se guarda junto con los costos (un solo botón)
    pct_guardado = None
    pct_bruto = request.form.get("pct_general", "").replace(",", ".").strip()
    if pct_bruto:
        try:
            pct_valor = float(pct_bruto)
            if 0 <= pct_valor <= 100:
                conn.execute("INSERT OR REPLACE INTO ajustes (clave, valor) VALUES ('costo_estimado_pct', ?)",
                             (str(pct_valor),))
                pct_guardado = pct_valor
        except ValueError:
            pass
    for pf in conn.execute("SELECT id, precio FROM perfumes").fetchall():
        bruto = request.form.get(f"costo_{pf['id']}")
        if bruto is None:
            continue
        bruto = bruto.replace(",", ".").strip()
        pct_txt = request.form.get(f"pct_{pf['id']}", "").replace(",", ".").strip()
        try:
            if bruto:
                valor = float(bruto)
            elif pct_txt:                                   # sin costo en pesos: se usa el % del precio
                valor = round(pf["precio"] * float(pct_txt) / 100, 2)
            else:
                valor = 0.0
        except ValueError:
            continue
        if valor < 0:
            continue
        conn.execute("UPDATE perfumes SET costo = ? WHERE id = ?", (valor, pf["id"]))
        cambios += 1
    conn.commit()
    conn.close()
    if pct_bruto and pct_guardado is None:
        flash("Costos guardados, pero el porcentaje debe estar entre 0 y 100; se conservó el anterior.")
    else:
        flash("Costos y porcentaje guardados." if cambios or pct_guardado is not None else "No se guardó ningún cambio.")
    return redirect(url_for("admin_inventario"))


@app.route("/admin/inventario/estimado", methods=["POST"])
@admin_requerido
def inventario_estimado():
    try:
        valor = float(request.form["pct"].replace(",", "."))
    except (ValueError, KeyError):
        flash("Escribe un porcentaje entre 0 y 100.")
        return redirect(url_for("admin_inventario"))
    if valor < 0 or valor > 100:
        flash("El porcentaje debe estar entre 0 y 100.")
        return redirect(url_for("admin_inventario"))
    conn = conectar()
    conn.execute("INSERT OR REPLACE INTO ajustes (clave, valor) VALUES ('costo_estimado_pct', ?)", (str(valor),))
    conn.commit()
    conn.close()
    flash("Costo estimado guardado: " + (f"{valor:g}% del precio de venta." if valor else "desactivado."))
    return redirect(url_for("admin_inventario"))


@app.route("/admin/inventario/<int:id>/eliminar", methods=["POST"])
@admin_requerido
def entrada_eliminar(id):
    conn = conectar()
    c = conn.execute("SELECT * FROM compras WHERE id = ?", (id,)).fetchone()
    if c is None:
        conn.close()
        abort(404)
    conn.execute("DELETE FROM compras WHERE id = ?", (id,))
    conn.execute("UPDATE perfumes SET stock = MAX(stock - ?, 0) WHERE id = ?", (c["cantidad"], c["perfume_id"]))
    conn.commit()
    conn.close()
    flash("Entrada eliminada y piezas restadas del inventario.")
    return redirect(url_for("admin_inventario"))


def _datos_reporte():
    """Calcula todos los datos del reporte según ?periodo=... (se usa en pantalla y en PDF)."""
    hoy = datetime.now().date()
    periodo = request.args.get("periodo", "30d")
    conn = conectar()

    # 1) Rango de fechas según el periodo elegido
    desde = hasta = hoy
    if periodo == "hoy":
        pass
    elif periodo == "7d":
        desde = hoy - timedelta(days=6)
    elif periodo == "mes":
        desde = hoy.replace(day=1)
    elif periodo == "anio":
        desde = hoy.replace(month=1, day=1)
    elif periodo == "todo":
        primera = conn.execute("SELECT MIN(substr(fecha,1,10)) f FROM ventas_pagadas").fetchone()["f"]
        desde = date.fromisoformat(primera) if primera else hoy
    elif periodo == "personalizado":
        try:
            desde = date.fromisoformat(request.args.get("desde", ""))
            hasta = date.fromisoformat(request.args.get("hasta", ""))
            if desde > hasta:
                desde, hasta = hasta, desde
        except ValueError:
            flash("Escribe las dos fechas para el periodo personalizado.")
            periodo, desde, hasta = "30d", hoy - timedelta(days=29), hoy
    else:
        periodo = "30d"
        desde = hoy - timedelta(days=29)
    d1, d2 = desde.isoformat(), hasta.isoformat()

    # 2) Resumen del periodo
    resumen = conn.execute("""SELECT COUNT(*) pedidos, COALESCE(SUM(total),0) ingresos
                              FROM ventas_pagadas WHERE substr(fecha,1,10) BETWEEN ? AND ?""", (d1, d2)).fetchone()
    unidades = conn.execute("""SELECT COALESCE(SUM(d.cantidad),0) n FROM detalle_ventas d
                               JOIN ventas_pagadas v ON v.id = d.venta_id
                               WHERE substr(v.fecha,1,10) BETWEEN ? AND ?""", (d1, d2)).fetchone()["n"]

    # 3) Ranking de perfumes (también aparecen los que no se vendieron)
    ranking = [dict(r) for r in conn.execute("""
        SELECT p.id, p.nombre, p.marca,
               COALESCE(SUM(x.cantidad), 0) unidades,
               COALESCE(SUM(x.cantidad * x.precio_unitario), 0) ingresos
        FROM perfumes p
        LEFT JOIN (SELECT d.perfume_id, d.cantidad, d.precio_unitario
                   FROM detalle_ventas d JOIN ventas_pagadas v ON v.id = d.venta_id
                   WHERE substr(v.fecha,1,10) BETWEEN ? AND ?) x ON x.perfume_id = p.id
        GROUP BY p.id ORDER BY unidades DESC, p.nombre""", (d1, d2)).fetchall()]
    max_u = max([r["unidades"] for r in ranking] or [0])
    for r in ranking:
        r["pct"] = round(r["unidades"] / max_u * 100) if max_u else 0
    mas_vendido = ranking[0] if ranking and ranking[0]["unidades"] > 0 else None
    menos_vendido = ranking[-1] if len(ranking) > 1 and mas_vendido else None

    # 4) Ventas por día (rangos cortos) o por mes (rangos largos)
    por_mes = (hasta - desde).days + 1 > 62
    largo = 7 if por_mes else 10
    filas = {r["k"]: r for r in conn.execute(
        f"""SELECT substr(fecha,1,{largo}) k, SUM(total) total, COUNT(*) pedidos
            FROM ventas_pagadas WHERE substr(fecha,1,10) BETWEEN ? AND ? GROUP BY k""", (d1, d2)).fetchall()}
    lineas, lotes, perf = _peps(conn)
    pct_estimado = _porcentaje_estimado(conn)
    ultimo_costo = {r["perfume_id"]: r["costo_unitario"] for r in
                    conn.execute("SELECT perfume_id, costo_unitario FROM compras ORDER BY fecha, id")}
    compras_periodo = [dict(r) for r in conn.execute("""
        SELECT c.id, c.fecha, c.cantidad, c.costo_unitario, c.proveedor, p.nombre, p.precio
        FROM compras c JOIN perfumes p ON p.id = c.perfume_id
        WHERE c.fecha BETWEEN ? AND ? ORDER BY c.fecha, c.id""", (d1, d2)).fetchall()]
    conn.close()
    for c_ in compras_periodo:
        c_["pct"] = (c_["costo_unitario"] / c_["precio"] * 100) if c_["precio"] else None

    # Costo y ganancia (método PEPS) de lo vendido en el periodo
    en_rango = [l for l in lineas if d1 <= l["fecha"] <= d2]
    costo_total = sum(l["costo"] for l in en_rango)
    ingreso_lineas = sum(l["ingreso"] for l in en_rango)
    resumen = dict(resumen)
    resumen["costo"] = costo_total
    resumen["ganancia"] = ingreso_lineas - costo_total
    resumen["margen"] = (resumen["ganancia"] / ingreso_lineas * 100) if ingreso_lineas else 0
    resumen["estimado"] = any(l["estimado"] for l in en_rango)
    for r in ranking:
        r["costo"] = sum(l["costo"] for l in en_rango if l["perfume_id"] == r["id"])
        r["ganancia"] = r["ingresos"] - r["costo"]
        r["estimado"] = any(l["estimado"] for l in en_rango if l["perfume_id"] == r["id"])
    vendidos_r = [r for r in ranking if r["unidades"] > 0]
    mas_rentable = max(vendidos_r, key=lambda r: r["ganancia"]) if vendidos_r else None
    costo_por = {}
    for l in en_rango:
        k = l["fecha"][:7] if por_mes else l["fecha"]
        costo_por[k] = costo_por.get(k, 0.0) + l["costo"]
    sin_costo_nombres = sorted({perf[l["perfume_id"]]["nombre"] for l in en_rango
                                if l["sin_costo"] and l["perfume_id"] in perf})
    estimado_nombres = sorted({perf[l["perfume_id"]]["nombre"] for l in en_rango
                               if l["estimado"] and l["perfume_id"] in perf})
    # Detalle PEPS: de qué entrada de inventario salieron las piezas vendidas en el periodo
    acum = {}
    for l in en_rango:
        for origen, orden, piezas, costo_u, est in l["consumos"]:
            k = (l["perfume_id"], orden, origen, round(costo_u, 4), est)
            acum[k] = acum.get(k, 0) + piezas
    peps_detalle = []
    for (pid_, orden, origen, costo_u, est), piezas in acum.items():
        pr = perf[pid_]["precio"] if pid_ in perf else 0
        peps_detalle.append({"nombre": perf[pid_]["nombre"] if pid_ in perf else "?", "orden": orden,
                             "origen": origen, "piezas": piezas, "costo": costo_u, "total": piezas * costo_u,
                             "pct": (costo_u / pr * 100) if pr else None, "estimado": est})
    peps_detalle.sort(key=lambda x: (x["nombre"], x["orden"]))
    # Inventario restante, entrada por entrada
    inventario_lotes = []
    for pid_, ls in lotes.items():
        pr = perf[pid_]["precio"] if pid_ in perf else 0
        for q, c_, o, cid, est in ls:
            if q > 0:
                inventario_lotes.append({"nombre": perf[pid_]["nombre"], "origen": o, "orden": cid or 0,
                                         "piezas": q, "costo": c_, "valor": q * c_,
                                         "pct": (c_ / pr * 100) if pr else None, "estimado": est})
    inventario_lotes.sort(key=lambda x: (x["nombre"], x["orden"]))

    # Ganancia por pieza de cada perfume (con precios y costos de hoy)
    por_pieza = []
    for pid, pf in perf.items():
        activos = [l for l in lotes.get(pid, []) if l[0] > 0]
        if activos:
            costo_p, est_p = activos[0][1], activos[0][4]      # costo de la próxima pieza que se vende
        elif pid in ultimo_costo:                              # sin existencia: costo de la última entrada
            costo_p, est_p = ultimo_costo[pid], False
        elif pf["costo"] > 0:
            costo_p, est_p = pf["costo"], False
        elif pct_estimado > 0:
            costo_p, est_p = pf["precio"] * pct_estimado / 100, True
        else:
            costo_p, est_p = None, False
        gan_p = (pf["precio"] - costo_p) if costo_p is not None else None
        por_pieza.append({"nombre": pf["nombre"], "marca": pf["marca"], "precio": pf["precio"],
                          "costo": costo_p, "ganancia": gan_p, "estimado": est_p, "stock": pf["stock"],
                          "margen": (gan_p / pf["precio"] * 100) if (gan_p is not None and pf["precio"]) else None,
                          "ganancia_inv": sum(l[0] * (pf["precio"] - l[1]) for l in activos)})
    por_pieza.sort(key=lambda x: (x["ganancia"] is None, -(x["ganancia"] or 0), x["nombre"]))
    inv_costo = sum(q * c for ls in lotes.values() for q, c, o, cid, est in ls)
    inv_piezas = sum(q for ls in lotes.values() for q, c, o, cid, est in ls)
    inv_estimado = any(est and q > 0 for ls in lotes.values() for q, c, o, cid, est in ls)

    claves, etiquetas = [], {}
    if por_mes:
        a, m = desde.year, desde.month
        while (a, m) <= (hasta.year, hasta.month):
            k = f"{a:04d}-{m:02d}"
            claves.append(k)
            etiquetas[k] = f"{MESES_ES[m - 1]} {a}"
            m += 1
            if m == 13:
                a, m = a + 1, 1
    else:
        dia = desde
        while dia <= hasta:
            k = dia.isoformat()
            claves.append(k)
            etiquetas[k] = dia.strftime("%d/%m")
            dia += timedelta(days=1)

    periodos = []
    for k in claves:
        f = filas.get(k)
        total_k = float(f["total"]) if f else 0.0
        periodos.append({"etiqueta": etiquetas[k],
                         "total": total_k,
                         "pedidos": f["pedidos"] if f else 0,
                         "costo": costo_por.get(k, 0.0),
                         "ganancia": total_k - costo_por.get(k, 0.0)})
    max_t = max([x["total"] for x in periodos] or [0])
    for x in periodos:
        x["pct"] = max(round(x["total"] / max_t * 100), 3) if x["total"] > 0 else 0
    con_ventas = [x for x in periodos if x["total"] > 0]
    mejor = max(con_ventas, key=lambda x: x["total"]) if con_ventas else None
    peor = min(con_ventas, key=lambda x: x["total"]) if len(con_ventas) > 1 else None
    sin_ventas = len(periodos) - len(con_ventas)

    return dict(periodo=periodo, desde=d1, hasta=d2,
                resumen=resumen, unidades=unidades, ranking=ranking,
                mas_vendido=mas_vendido, menos_vendido=menos_vendido,
                periodos=periodos, por_mes=por_mes, mejor=mejor, peor=peor,
                sin_ventas=sin_ventas, mas_rentable=mas_rentable,
                sin_costo_nombres=sin_costo_nombres, inv_costo=inv_costo, inv_piezas=inv_piezas,
                estimado_nombres=estimado_nombres, pct_estimado=pct_estimado, inv_estimado=inv_estimado,
                por_pieza=por_pieza, peps_detalle=peps_detalle, compras_periodo=compras_periodo,
                inventario_lotes=inventario_lotes)


@app.route("/admin/reportes")
@admin_requerido
def admin_reportes():
    return render_template("admin_reportes.html", **_datos_reporte())


NOMBRES_PERIODO = {"hoy": "Hoy", "7d": "Últimos 7 días", "30d": "Últimos 30 días",
                   "mes": "Este mes", "anio": "Este año", "todo": "Todo el historial",
                   "personalizado": "Periodo personalizado"}


def _fecha_mx(iso):
    """2026-11-06 -> 06/11/2026"""
    a, m, d = iso.split("-")
    return f"{d}/{m}/{a}"


@app.route("/admin/reportes/pdf")
@admin_requerido
def admin_reportes_pdf():
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                        Table, TableStyle, KeepTogether)
        from reportlab.graphics.shapes import Drawing, Rect, String
        from reportlab.graphics.charts.barcharts import VerticalBarChart
        from xml.sax.saxutils import escape
    except ImportError:
        flash("Para descargar el PDF falta instalar 'reportlab'. Cierra el sistema y vuelve a abrir INICIAR_WINDOWS.")
        return redirect(url_for("admin_reportes"))

    d = _datos_reporte()
    oscuro = colors.HexColor("#2f2630")
    rosa = colors.HexColor("#9c3d5b")
    azul = colors.HexColor("#123f73")
    verde = "#1e7a46"
    rojo = "#a02323"
    gris = colors.HexColor("#6b6270")
    linea = colors.HexColor("#e4dcd9")

    est = getSampleStyleSheet()
    s_titulo = ParagraphStyle("t", parent=est["Title"], fontName="Helvetica-Bold", fontSize=22,
                              leading=28, textColor=oscuro, alignment=0, spaceAfter=6)
    s_sub = ParagraphStyle("s", parent=est["Normal"], fontSize=10.5, textColor=gris, spaceAfter=2)
    s_h = ParagraphStyle("h", parent=est["Heading2"], fontName="Helvetica-Bold", fontSize=13,
                         textColor=oscuro, spaceBefore=16, spaceAfter=6)
    s_celda = ParagraphStyle("c", parent=est["Normal"], fontSize=9, leading=11)
    s_der = ParagraphStyle("d", parent=s_celda, alignment=2)
    s_nota = ParagraphStyle("n", parent=est["Normal"], fontSize=8.5, leading=11, textColor=gris, spaceBefore=4)
    s_info = ParagraphStyle("i", parent=est["Normal"], fontSize=9, leading=12,
                            textColor=colors.HexColor("#1b4670"), backColor=colors.HexColor("#e6f0fb"),
                            borderPadding=6, spaceBefore=8, spaceAfter=4)
    s_aviso = ParagraphStyle("a", parent=est["Normal"], fontSize=9, leading=12,
                             textColor=colors.HexColor("#7a4b00"), backColor=colors.HexColor("#fff4dc"),
                             borderPadding=6, spaceBefore=8, spaceAfter=4)

    def dinero(v):
        return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"

    def ganancia_txt(v, negrita=False, derecha=True, estimado=False):
        color = rojo if v < 0 else verde
        t = dinero(v) + (" *" if estimado else "")
        return Paragraph(f'<font color="{color}">{"<b>" if negrita else ""}{t}{"</b>" if negrita else ""}</font>',
                         s_der if derecha else s_celda)

    def tabla(datos, anchos, alinear_der=()):
        t = Table(datos, colWidths=anchos, repeatRows=1)
        estilo = [("BACKGROUND", (0, 0), (-1, 0), oscuro),
                  ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                  ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                  ("FONTSIZE", (0, 0), (-1, -1), 9),
                  ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#faf6f5")]),
                  ("LINEBELOW", (0, 0), (-1, -1), 0.4, linea),
                  ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                  ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
        for c in alinear_der:
            estilo.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
        t.setStyle(TableStyle(estilo))
        return t

    ancho = letter[0] - 1.5 * inch
    unidad = "Mes" if d["por_mes"] else "Día"
    historia = []

    historia.append(Paragraph("FRAG-IA · Reporte de ventas", s_titulo))
    historia.append(Paragraph(
        f"<b>{NOMBRES_PERIODO.get(d['periodo'], '')}</b> · del {_fecha_mx(d['desde'])} al {_fecha_mx(d['hasta'])}", s_sub))
    historia.append(Paragraph(f"Generado el {datetime.now().strftime('%d/%m/%Y %H:%M')}", s_sub))

    if d["sin_costo_nombres"]:
        historia.append(Paragraph(
            "<b>Atención:</b> estos perfumes se vendieron sin tener costo registrado, por eso su ganancia "
            "aparece más alta de lo real: " + escape(", ".join(d["sin_costo_nombres"])) +
            ". Captura su costo en Inventario.", s_aviso))

    if d["estimado_nombres"]:
        historia.append(Paragraph(
            f"<b>Ganancia estimada:</b> estos perfumes no tienen costo real capturado, por eso se supuso un costo de "
            f"{d['pct_estimado']:g}% de su precio de venta: " + escape(", ".join(d["estimado_nombres"])) +
            ". Captura su costo real en Inventario para que la ganancia sea exacta.", s_info))

    # Resumen
    r = d["resumen"]
    prom = dinero(r["ingresos"] / r["pedidos"]) if r["pedidos"] else "—"
    mas = f"{d['mas_vendido']['nombre']} ({d['mas_vendido']['unidades']})" if d["mas_vendido"] else "Sin ventas"
    if d["menos_vendido"]:
        u = d["menos_vendido"]["unidades"]
        menos = f"{d['menos_vendido']['nombre']} ({'no se vendió' if u == 0 else u})"
    else:
        menos = "Sin datos"
    rentable = (f"{d['mas_rentable']['nombre']} ({dinero(d['mas_rentable']['ganancia'])})"
                if d["mas_rentable"] else "Sin ventas")
    mejor = f"{d['mejor']['etiqueta']} · {dinero(d['mejor']['total'])}" if d["mejor"] else "Sin ventas"
    peor = f"{d['peor']['etiqueta']} · {dinero(d['peor']['total'])}" if d["peor"] else "Sin datos"
    filas = [["Concepto", "Resultado"],
             ["Ingresos por ventas", dinero(r["ingresos"])],
             ["Costo de lo vendido (lo que se pagó al proveedor)", dinero(r["costo"])],
             [Paragraph("<b>GANANCIA</b>" + (" (incluye costo estimado)" if r["estimado"] else ""), s_celda),
             ganancia_txt(r["ganancia"], True, derecha=False)],
             ["Margen de ganancia", f"{r['margen']:.1f}%"],
             ["Pedidos", str(r["pedidos"])],
             ["Promedio por pedido", prom],
             ["Perfumes vendidos", str(d["unidades"])],
             ["Perfume más vendido", Paragraph(escape(mas), s_celda)],
             ["Perfume menos vendido", Paragraph(escape(menos), s_celda)],
             ["Perfume que más ganancia dejó", Paragraph(escape(rentable), s_celda)],
             [f"{unidad} con más ventas", mejor],
             [f"{unidad} con menos ventas", peor],
             [f"Inventario restante ({d['inv_piezas']} piezas, a costo{', con estimados' if d['inv_estimado'] else ''})",
              dinero(d["inv_costo"])]]
    historia.append(Paragraph("Resumen", s_h))
    historia.append(tabla(filas, [ancho * 0.55, ancho * 0.45]))

    # Gráfica de ventas y ganancia por día / mes
    if d["mejor"]:
        pr = d["periodos"]
        paso = max(1, len(pr) // 12)
        dib = Drawing(ancho, 192)
        g = VerticalBarChart()
        g.x, g.y, g.width, g.height = 50, 28, ancho - 70, 128
        g.data = [[x["total"] for x in pr], [x["ganancia"] for x in pr]]
        g.categoryAxis.categoryNames = [x["etiqueta"] if i % paso == 0 else "" for i, x in enumerate(pr)]
        g.categoryAxis.labelAxisMode = "low"
        g.categoryAxis.labels.fontName = "Helvetica"
        g.categoryAxis.labels.fontSize = 7
        g.categoryAxis.labels.angle = 0
        g.valueAxis.labels.fontName = "Helvetica"
        g.valueAxis.labels.fontSize = 7
        g.valueAxis.valueMin = min(0, min(x["ganancia"] for x in pr))
        g.valueAxis.labelTextFormat = lambda v: f"${v:,.0f}"
        g.bars[0].fillColor = rosa
        g.bars[1].fillColor = azul
        g.bars[0].strokeColor = None
        g.bars[1].strokeColor = None
        g.barWidth = max(2, min(12, (ancho - 70) / max(len(pr), 1) * 0.32))
        g.groupSpacing = 3
        dib.add(g)
        # Leyenda dibujada dentro de la gráfica
        dib.add(Rect(50, 176, 9, 9, fillColor=rosa, strokeColor=None))
        dib.add(String(64, 177, "Ventas", fontName="Helvetica-Bold", fontSize=8.5, fillColor=rosa))
        dib.add(Rect(115, 176, 9, 9, fillColor=azul, strokeColor=None))
        dib.add(String(129, 177, "Ganancia", fontName="Helvetica-Bold", fontSize=8.5, fillColor=azul))
        historia.append(KeepTogether([
            Paragraph(f"Ventas y ganancia por {'mes' if d['por_mes'] else 'día'}", s_h), dib]))

    # Ranking de perfumes
    historia.append(Paragraph("Perfumes: de más a menos vendido", s_h))
    if d["ranking"]:
        filas = [["#", "Perfume", "Vendidos", "Ingresos", "Costo", "Ganancia"]]
        for i, x in enumerate(d["ranking"], 1):
            nombre = escape(x["nombre"])
            if x["marca"]:
                nombre += f'<br/><font size="7" color="#6b6270">{escape(x["marca"])}</font>'
            filas.append([str(i), Paragraph(nombre, s_celda), str(x["unidades"]),
                          dinero(x["ingresos"]), dinero(x["costo"]),
                          ganancia_txt(x["ganancia"], estimado=x["estimado"])])
        historia.append(tabla(filas, [ancho * 0.06, ancho * 0.32, ancho * 0.11, ancho * 0.17,
                                      ancho * 0.16, ancho * 0.18], alinear_der=(2, 3, 4, 5)))
        if d["estimado_nombres"]:
            historia.append(Paragraph(
                f"* Ganancia estimada: sin costo real capturado, se supuso un costo de {d['pct_estimado']:g}% del precio de venta.", s_nota))
    else:
        historia.append(Paragraph("Todavía no hay perfumes registrados.", s_nota))

    # Ganancia por pieza de cada perfume
    historia.append(Paragraph("Cuánto deja cada perfume por pieza (precios y costos de hoy)", s_h))
    if d["por_pieza"]:
        filas = [["Perfume", "Precio", "Costo", "Ganancia", "Margen", "Piezas", "Gan. inventario"]]
        for x in d["por_pieza"]:
            nombre = escape(x["nombre"])
            if x["marca"]:
                nombre += f'<br/><font size="7" color="#6b6270">{escape(x["marca"])}</font>'
            filas.append([Paragraph(nombre, s_celda), dinero(x["precio"]),
                          dinero(x["costo"]) + (" *" if x["estimado"] else "") if x["costo"] is not None else "Sin costo",
                          ganancia_txt(x["ganancia"], estimado=x["estimado"]) if x["ganancia"] is not None else "—",
                          f"{x['margen']:.1f}%" if x["margen"] is not None else "—",
                          str(x["stock"]), dinero(x["ganancia_inv"])])
        historia.append(tabla(filas, [ancho * 0.25, ancho * 0.13, ancho * 0.13, ancho * 0.14,
                                      ancho * 0.09, ancho * 0.09, ancho * 0.17], alinear_der=(1, 2, 3, 4, 5, 6)))
        historia.append(Paragraph(
            "Ganancia = precio de venta - costo de la próxima pieza que se vende. "
            "Gan. inventario = lo que dejaría vender todas las piezas que hay en existencia."
            + (f" * Costo estimado ({d['pct_estimado']:g}% del precio)." if any(x["estimado"] for x in d["por_pieza"]) else ""),
            s_nota))

    def pct_txt(v):
        return f"{v:.1f}%" if v is not None else "—"

    def origen_par(o, est):
        return Paragraph(escape(o) + (" <font color='#6b6270'>(estimado)</font>" if est else ""), s_celda)

    # Método PEPS: de qué entrada de inventario salió lo vendido
    historia.append(Paragraph("Cómo se calculó el costo de lo vendido (método PEPS)", s_h))
    if d["peps_detalle"]:
        filas = [["Perfume", "Salió de", "Piezas", "Costo c/u", "% del precio", "Costo total"]]
        for x in d["peps_detalle"]:
            filas.append([Paragraph(escape(x["nombre"]), s_celda), origen_par(x["origen"], x["estimado"]),
                          str(x["piezas"]), dinero(x["costo"]), pct_txt(x["pct"]), dinero(x["total"])])
        historia.append(tabla(filas, [ancho * 0.27, ancho * 0.23, ancho * 0.10, ancho * 0.14,
                                      ancho * 0.12, ancho * 0.14], alinear_der=(2, 3, 4, 5)))
        historia.append(Paragraph(
            "Las piezas se descuentan de la entrada de inventario más antigua que todavía tenía existencia. "
            "% del precio = costo ÷ precio de venta actual del perfume.", s_nota))
    else:
        historia.append(Paragraph("No hubo ventas en este periodo.", s_nota))

    # Entradas de inventario del periodo
    historia.append(Paragraph("Entradas de inventario del periodo", s_h))
    if d["compras_periodo"]:
        filas = [["Fecha", "Perfume", "Piezas", "Costo c/u", "% del precio", "Total", "Proveedor"]]
        for x in d["compras_periodo"]:
            filas.append([_fecha_mx(x["fecha"]), Paragraph(escape(x["nombre"]), s_celda), str(x["cantidad"]),
                          dinero(x["costo_unitario"]), pct_txt(x["pct"]),
                          dinero(x["cantidad"] * x["costo_unitario"]),
                          Paragraph(escape(x["proveedor"] or "—"), s_celda)])
        historia.append(tabla(filas, [ancho * 0.12, ancho * 0.24, ancho * 0.08, ancho * 0.13,
                                      ancho * 0.11, ancho * 0.14, ancho * 0.18], alinear_der=(2, 3, 4, 5)))
    else:
        historia.append(Paragraph("No se registraron entradas de inventario en este periodo.", s_nota))

    # Inventario restante entrada por entrada
    historia.append(Paragraph("Inventario restante por entrada", s_h))
    if d["inventario_lotes"]:
        filas = [["Perfume", "Entrada", "Piezas", "Costo c/u", "% del precio", "Valor a costo"]]
        for x in d["inventario_lotes"]:
            filas.append([Paragraph(escape(x["nombre"]), s_celda), origen_par(x["origen"], x["estimado"]),
                          str(x["piezas"]), dinero(x["costo"]), pct_txt(x["pct"]), dinero(x["valor"])])
        historia.append(tabla(filas, [ancho * 0.27, ancho * 0.23, ancho * 0.10, ancho * 0.14,
                                      ancho * 0.12, ancho * 0.14], alinear_der=(2, 3, 4, 5)))
    else:
        historia.append(Paragraph("No quedan piezas en inventario.", s_nota))

    # Detalle por día / mes (solo los que tuvieron ventas)
    historia.append(Paragraph(f"Detalle por {'mes' if d['por_mes'] else 'día'}", s_h))
    con = [x for x in d["periodos"] if x["total"] > 0]
    if con:
        filas = [[unidad, "Pedidos", "Ventas", "Costo", "Ganancia"]]
        for x in con:
            filas.append([x["etiqueta"], str(x["pedidos"]), dinero(x["total"]), dinero(x["costo"]),
                          ganancia_txt(x["ganancia"])])
        historia.append(tabla(filas, [ancho * 0.22, ancho * 0.14, ancho * 0.22, ancho * 0.21, ancho * 0.21],
                              alinear_der=(1, 2, 3, 4)))
        if d["sin_ventas"]:
            plural = "es" if d["por_mes"] and d["sin_ventas"] != 1 else ("s" if d["sin_ventas"] != 1 else "")
            historia.append(Paragraph(
                f"{d['sin_ventas']} {unidad.lower()}{plural} sin ventas en este periodo (no se muestran).", s_nota))
    else:
        historia.append(Paragraph("No hubo ventas en este periodo.", s_nota))

    historia.append(Paragraph(
        "<b>Cómo se calcula la ganancia:</b> con el método PEPS (primeras entradas, primeras salidas). "
        "Cada perfume vendido se descuenta de la entrada de inventario más antigua que todavía tenga piezas, y su costo es "
        "el precio que se pagó al proveedor en esa entrada. Ganancia = ventas - costo. Cuando un perfume no tiene "
        "costo real capturado, se estima un porcentaje de su precio de venta y se marca como estimado.", s_nota))

    def pie(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(gris)
        canvas.drawString(0.75 * inch, 0.5 * inch, "FRAG-IA · Reporte de ventas")
        canvas.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch, f"Página {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.7 * inch, bottomMargin=0.8 * inch,
                            title="Reporte de ventas FRAG-IA", author="FRAG-IA")
    doc.build(historia, onFirstPage=pie, onLaterPages=pie)
    buf.seek(0)
    nombre = f"reporte_ventas_{d['desde']}_a_{d['hasta']}.pdf"
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=nombre)


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

@app.route("/pedido/<int:id>/recibido", methods=["POST"])
@cliente_requerido
def pedido_recibido(id):
    conn = conectar()
    venta = conn.execute("SELECT estado FROM ventas WHERE id = ? AND usuario_id = ?",
                         (id, session["usuario_id"])).fetchone()
    if venta is None:
        conn.close()
        abort(404)
    if venta["estado"] != "Entregado":
        flash("Solo puedes confirmar un pedido cuando ya fue entregado.")
    else:
        conn.execute("UPDATE ventas SET estado = 'Recibido' WHERE id = ?", (id,))
        conn.commit()
        flash("¡Gracias! Confirmaste que recibiste tu pedido.")
    conn.close()
    return redirect(url_for("mis_pedidos"))


@app.errorhandler(413)
def archivo_muy_grande(e):
    flash("El archivo pesa más de 5 MB. Sube uno más ligero.")
    destino = url_for("admin_perfumes") if session.get("rol") == "admin" else url_for("mis_pedidos")
    return redirect(destino)


def _devolver_stock(conn, venta_id):
    """Regresa al inventario las piezas apartadas por un pedido que no se pagó."""
    for d in conn.execute("SELECT perfume_id, cantidad FROM detalle_ventas WHERE venta_id = ?",
                          (venta_id,)).fetchall():
        conn.execute("UPDATE perfumes SET stock = stock + ? WHERE id = ?",
                     (d["cantidad"], d["perfume_id"]))


@app.route("/pedido/<int:id>/comprobante", methods=["POST"])
@cliente_requerido
def subir_comprobante(id):
    conn = conectar()
    venta = conn.execute("SELECT estado, comprobante FROM ventas WHERE id = ? AND usuario_id = ?",
                         (id, session["usuario_id"])).fetchone()
    if venta is None:
        conn.close()
        abort(404)
    if venta["estado"] not in ESTADOS_SIN_PAGO:
        conn.close()
        flash("Este pedido ya no admite comprobante.")
        return redirect(url_for("compra_exitosa", id=id))
    nombre = guardar_comprobante(request.files.get("comprobante"))
    if nombre is None:
        conn.close()
        flash("Sube tu comprobante como PDF, JPG, PNG o WEBP (máximo 5 MB).")
        return redirect(url_for("compra_exitosa", id=id))
    borrar_comprobante(venta["comprobante"])      # si ya había uno, se reemplaza
    conn.execute("""UPDATE ventas SET comprobante = ?, comprobante_fecha = ?,
                    estado = 'Comprobante enviado', motivo_rechazo = NULL WHERE id = ?""",
                 (nombre, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), id))
    conn.commit()
    conn.close()
    flash("¡Comprobante enviado! Revisaremos tu pago y te avisaremos.")
    return redirect(url_for("compra_exitosa", id=id))


@app.route("/pedido/<int:id>/cancelar", methods=["POST"])
@cliente_requerido
def cancelar_pedido(id):
    conn = conectar()
    try:
        conn.execute("BEGIN IMMEDIATE")
        v = conn.execute("SELECT estado FROM ventas WHERE id = ? AND usuario_id = ?",
                         (id, session["usuario_id"])).fetchone()
        if v is None:
            conn.rollback()
            abort(404)
        if v["estado"] not in ("Pendiente de pago", "Pago rechazado"):
            conn.rollback()
            flash("Este pedido ya no se puede cancelar.")
        else:
            _devolver_stock(conn, id)
            conn.execute("UPDATE ventas SET estado = 'Cancelado' WHERE id = ?", (id,))
            conn.commit()
            flash("Pedido cancelado.")
    finally:
        conn.close()
    return redirect(url_for("mis_pedidos"))


@app.route("/comprobante/<int:id>")
def ver_comprobante(id):
    """Muestra el comprobante solo al administrador o al cliente dueño del pedido."""
    if "usuario_id" not in session:
        abort(403)
    conn = conectar()
    v = conn.execute("SELECT usuario_id, comprobante FROM ventas WHERE id = ?", (id,)).fetchone()
    conn.close()
    if v is None or not v["comprobante"]:
        abort(404)
    if session.get("rol") != "admin" and v["usuario_id"] != session["usuario_id"]:
        abort(404)
    ruta = os.path.join(CARPETA_COMPROBANTES, os.path.basename(v["comprobante"]))
    if not os.path.exists(ruta):
        abort(404)
    resp = send_file(ruta)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


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

        elif accion == "direccion":
            f = request.form
            errores = []
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
            if errores:
                flash(" ".join(errores))
            else:
                conn.execute(
                    """UPDATE usuarios SET dir_telefono = ?, dir_calle = ?, dir_colonia = ?, dir_ciudad = ?,
                                           dir_estado = ?, dir_cp = ?, dir_referencias = ?
                       WHERE id = ?""",
                    (solo_digitos(f["telefono"]), f["direccion"].strip(), f["colonia"].strip(),
                     f["ciudad"].strip(), f["estado"], solo_digitos(f["cp"]),
                     f.get("referencias", "").strip(), usuario["id"]))
                conn.commit()
                flash("Dirección de envío guardada.")

        elif accion == "contrasena":
            conn.close()
            if not check_password_hash(usuario['contrasena'], request.form.get('actual','')):
                flash('La contraseña actual no es correcta.')
                return redirect(url_for('perfil'))
            if emitir_codigo(usuario, 'cambiar contraseña'):
                return redirect(url_for('verificar_correo'))
            flash('No se pudo enviar el código. Espera un minuto o revisa Gmail.')
            return redirect(url_for('perfil'))

        conn.close()
        return redirect(url_for("perfil"))

    resumen = conn.execute("SELECT COUNT(*) pedidos FROM ventas WHERE usuario_id = ?",
                           (usuario["id"],)).fetchone()
    ultimo = conn.execute("SELECT * FROM ventas WHERE usuario_id = ? ORDER BY id DESC LIMIT 1",
                          (usuario["id"],)).fetchone()
    conn.close()
    return render_template("perfil.html", usuario=usuario, resumen=resumen, ultimo=ultimo, estados=ESTADOS_MX)


@app.route("/admin/usuarios")
@admin_requerido
def admin_usuarios():
    conn = conectar()
    usuarios = conn.execute("""SELECT u.id,u.nombre,u.correo,u.rol,COUNT(v.id) compras,COALESCE(SUM(v.total),0) total FROM usuarios u LEFT JOIN ventas_pagadas v ON v.usuario_id=u.id GROUP BY u.id ORDER BY u.id DESC""").fetchall()
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
    if estado not in ("En proceso", "En camino", "Entregado"):
        abort(400)
    conn = conectar()
    actual = conn.execute("SELECT estado FROM ventas WHERE id=?", (id,)).fetchone()
    if actual is None:
        conn.close()
        abort(404)
    if actual["estado"] in ESTADOS_SIN_PAGO or actual["estado"] == "Cancelado":
        flash("Primero confirma el pago del pedido para poder actualizar su envío.")
    elif actual["estado"] == "Recibido":
        flash("Este pedido ya fue confirmado como recibido por el cliente.")
    else:
        conn.execute("UPDATE ventas SET estado=? WHERE id=?", (estado, id))
        conn.commit()
        flash("Estado actualizado")
    conn.close()
    return redirect(url_for("admin_pedidos"))

def _pedido_con_cliente(conn, id):
    return conn.execute("""SELECT v.id, v.estado, v.total, v.comprobante, v.envio_nombre, u.correo
                           FROM ventas v JOIN usuarios u ON u.id = v.usuario_id
                           WHERE v.id = ?""", (id,)).fetchone()


@app.route("/admin/pedidos/<int:id>/confirmar-pago", methods=["POST"])
@admin_requerido
def admin_confirmar_pago(id):
    conn = conectar()
    v = _pedido_con_cliente(conn, id)
    if v is None:
        conn.close()
        abort(404)
    if v["estado"] not in ESTADOS_SIN_PAGO:
        flash("Este pedido ya no está pendiente de pago.")
    else:
        conn.execute("""UPDATE ventas SET estado = 'En proceso', motivo_rechazo = NULL,
                        pago_confirmado_fecha = ? WHERE id = ?""",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), id))
        conn.commit()
        flash(f"Pago confirmado. El pedido #{id:04d} pasó a «En proceso».")
        avisar_cliente(v["correo"], f"Pago confirmado · pedido #{id} FRAG-IA",
                       f"Hola {v['envio_nombre'] or ''},\n\nConfirmamos tu pago de ${v['total']:,.2f} "
                       f"del pedido #{id}. Ya lo estamos preparando; puedes ver su estado en "
                       "Mis pedidos.\n\nFRAG-IA Perfumería")
    conn.close()
    return redirect(url_for("admin_pedidos"))


@app.route("/admin/pedidos/<int:id>/rechazar-pago", methods=["POST"])
@admin_requerido
def admin_rechazar_pago(id):
    motivo = request.form.get("motivo", "").strip()[:300]
    conn = conectar()
    v = _pedido_con_cliente(conn, id)
    if v is None:
        conn.close()
        abort(404)
    if not motivo:
        flash("Escribe el motivo del rechazo para que el cliente sepa qué corregir.")
    elif v["estado"] != "Comprobante enviado":
        flash("Solo se puede rechazar un pedido con comprobante enviado.")
    else:
        conn.execute("UPDATE ventas SET estado = 'Pago rechazado', motivo_rechazo = ? WHERE id = ?",
                     (motivo, id))
        conn.commit()
        flash(f"Comprobante del pedido #{id:04d} rechazado. El cliente puede subir otro.")
        avisar_cliente(v["correo"], f"Revisa tu comprobante · pedido #{id} FRAG-IA",
                       f"Hola {v['envio_nombre'] or ''},\n\nNo pudimos confirmar el pago de tu pedido #{id}.\n"
                       f"Motivo: {motivo}\n\nEntra a Mis pedidos y sube un comprobante correcto.\n\n"
                       "FRAG-IA Perfumería")
    conn.close()
    return redirect(url_for("admin_pedidos"))


@app.route("/admin/pedidos/<int:id>/cancelar", methods=["POST"])
@admin_requerido
def admin_cancelar_pedido(id):
    conn = conectar()
    try:
        conn.execute("BEGIN IMMEDIATE")
        v = conn.execute("SELECT estado FROM ventas WHERE id = ?", (id,)).fetchone()
        if v is None:
            conn.rollback()
            abort(404)
        if v["estado"] not in ESTADOS_SIN_PAGO:
            conn.rollback()
            flash("Solo se pueden cancelar pedidos que aún no tienen el pago confirmado.")
        else:
            _devolver_stock(conn, id)
            conn.execute("UPDATE ventas SET estado = 'Cancelado' WHERE id = ?", (id,))
            conn.commit()
            flash(f"Pedido #{id:04d} cancelado y stock devuelto al inventario.")
    finally:
        conn.close()
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
        d = conn.execute('SELECT COALESCE(SUM(cantidad),0) n, COALESCE(SUM(cantidad*precio_unitario),0) importe FROM detalle_pagado WHERE perfume_id=?',(p['id'],)).fetchone()
        return f"{p['nombre']} ({p['marca'] or 'sin marca'}): precio {money(p['precio'])}, stock {p['stock']} unidades, categoría {p['categoria']}; se vendieron {d['n']} unidades por {money(d['importe'])}."
    if has('mas vendido','mas popular','top','ranking','mejor vendido','menos vendido','mas comprado') or (has('vendido') and has('cual','que','perfume','producto')):
        ascending = has('menos','peor')
        xs = conn.execute('SELECT p.nombre,SUM(d.cantidad) unidades,SUM(d.cantidad*d.precio_unitario) ingresos FROM detalle_pagado d JOIN perfumes p ON p.id=d.perfume_id GROUP BY p.id ORDER BY unidades '+('ASC' if ascending else 'DESC')+' LIMIT 5').fetchall()
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
        x=conn.execute('SELECT COUNT(*) n,COALESCE(SUM(total),0) total FROM ventas_pagadas'+where).fetchone()
        return f"{label} se registraron {x['n']} ventas por {money(x['total'])} de ingresos brutos. La base de datos no contiene costos para calcular ganancias netas."
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
asegurar_costos()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")