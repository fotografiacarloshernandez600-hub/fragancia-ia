import sqlite3
from pathlib import Path
DB_PATH = Path(__file__).resolve().parent / "tienda.db"
from werkzeug.security import generate_password_hash

def conectar():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def crear_tablas():
    conn = conectar()
    cur = conn.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        correo TEXT UNIQUE NOT NULL,
        contrasena TEXT NOT NULL,
        rol TEXT NOT NULL DEFAULT 'cliente'
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS perfumes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        marca TEXT,
        categoria TEXT NOT NULL,
        precio REAL NOT NULL,
        stock INTEGER NOT NULL DEFAULT 0,
        imagen TEXT
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS ventas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        total REAL NOT NULL,
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS detalle_ventas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        venta_id INTEGER NOT NULL,
        perfume_id INTEGER NOT NULL,
        cantidad INTEGER NOT NULL,
        precio_unitario REAL NOT NULL,
        FOREIGN KEY (venta_id) REFERENCES ventas(id),
        FOREIGN KEY (perfume_id) REFERENCES perfumes(id)
    )""")

    columnas = [r[1] for r in conn.execute("PRAGMA table_info(ventas)").fetchall()]
    if "estado" not in columnas:
        conn.execute("ALTER TABLE ventas ADD COLUMN estado TEXT NOT NULL DEFAULT 'En proceso'")
    # Datos de envío y pago (se agregan solos a la base que ya tienes)
    for col in ("envio_nombre", "envio_telefono", "envio_direccion", "envio_ciudad",
                "envio_estado", "envio_cp", "envio_referencias", "metodo_pago"):
        if col not in columnas:
            conn.execute(f"ALTER TABLE ventas ADD COLUMN {col} TEXT")
    # Dirección de envío guardada de cada cliente (se agrega sola a la base que ya tienes)
    cols_u = [r[1] for r in conn.execute("PRAGMA table_info(usuarios)").fetchall()]
    for col in ("dir_telefono", "dir_calle", "dir_colonia", "dir_ciudad",
                "dir_estado", "dir_cp", "dir_referencias"):
        if col not in cols_u:
            conn.execute(f"ALTER TABLE usuarios ADD COLUMN {col} TEXT")
    # Estados del pedido: En proceso -> En camino -> Entregado -> Recibido (lo confirma el cliente).
    # Esto se hace una sola vez: los pedidos viejos pasan a "En proceso".
    if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
        conn.execute("UPDATE ventas SET estado = 'En proceso' WHERE estado IN ('Recibido', 'Preparando')")
        conn.execute("PRAGMA user_version = 1")
    # Comprobante de pago por transferencia (se agrega solo a la base que ya tienes)
    columnas = [r[1] for r in conn.execute("PRAGMA table_info(ventas)").fetchall()]
    for col in ("comprobante", "comprobante_fecha", "motivo_rechazo", "pago_confirmado_fecha"):
        if col not in columnas:
            conn.execute(f"ALTER TABLE ventas ADD COLUMN {col} TEXT")
    # Vistas con SOLO las ventas pagadas: los reportes y estadísticas usan estas, así un pedido
    # pendiente, rechazado o cancelado no cuenta como dinero ganado.
    pagados = "('En proceso','En camino','Entregado','Recibido')"
    conn.execute("DROP VIEW IF EXISTS ventas_pagadas")
    conn.execute(f"CREATE VIEW ventas_pagadas AS SELECT * FROM ventas WHERE estado IN {pagados}")
    conn.execute("DROP VIEW IF EXISTS detalle_pagado")
    conn.execute(f"""CREATE VIEW detalle_pagado AS SELECT d.* FROM detalle_ventas d
                     JOIN ventas v ON v.id = d.venta_id WHERE v.estado IN {pagados}""")
    conn.commit()
    conn.close()

def datos_ejemplo():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Solo agrega datos si la tabla está vacía
    cur.execute("SELECT COUNT(*) FROM usuarios")
    if cur.fetchone()[0] > 0:
        conn.close()
        return

    # Administrador y un cliente de prueba
    cur.execute("INSERT INTO usuarios (nombre, correo, contrasena, rol) VALUES (?, ?, ?, ?)",
                ("Administrador", "admin@fragia.com", generate_password_hash("admin123"), "admin"))
    cur.execute("INSERT INTO usuarios (nombre, correo, contrasena, rol) VALUES (?, ?, ?, ?)",
                ("Cliente Prueba", "cliente@fragia.com", generate_password_hash("cliente123"), "cliente"))

    # Perfumes de ejemplo (sin foto: se suben desde el panel del administrador)
    perfumes = [
        ("Miss Dior Eau de Parfum", "Dior", "mujer", 1999.59, 15, None),
        ("Born in Roma Coral", "Valentino", "mujer", 1999.59, 10, None),
        ("Le Beau EDP", "Jean Paul Gaultier", "hombre", 1999.59, 12, None),
        ("Bleu de Chanel", "Chanel", "hombre", 2499.00, 8, None),
        ("CK One", "Calvin Klein", "unisex", 899.00, 20, None),
    ]
    cur.executemany("""INSERT INTO perfumes (nombre, marca, categoria, precio, stock, imagen)
                       VALUES (?, ?, ?, ?, ?, ?)""", perfumes)

    conn.commit()
    conn.close()

if __name__ == "__main__":
    crear_tablas()
    datos_ejemplo()
    print("Base de datos lista")