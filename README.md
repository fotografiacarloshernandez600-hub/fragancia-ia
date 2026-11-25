# FRAG-IA — tienda de perfumes y asistente administrativo

Proyecto académico local construido sobre el prototipo original. Tecnologías: Python, Flask, SQLite, HTML, CSS y JavaScript.

## Cómo ejecutar (Windows)
1. Instala Python 3.10 o superior.
2. Extrae todo el ZIP.
3. Ejecuta `INICIAR_WINDOWS.bat` o abre una terminal en la carpeta y ejecuta `pip install -r requirements.txt` y `python app.py`.
4. Abre http://127.0.0.1:5000.

Si quieres iniciar con la base de datos de ejemplo desde cero, conserva una copia de `tienda.db`, elimínala de la carpeta del proyecto y ejecuta `python database.py`. **Esto borra las compras existentes si eliminas la base.**

## Cuentas de demostración (solo uso local)
- Administrador: `admin@fragia.com` / `admin123`
- Cliente: `cliente@fragia.com` / `cliente123`

Estas cuentas existen si se ejecutó la inicialización `python database.py` sobre una base vacía. Cambia las contraseñas antes de usarlo fuera de una demostración.

## Funcionalidad
- Catálogo con búsqueda, categorías, ordenamiento y carrito.
- Pago **solo por transferencia bancaria**: el cliente transfiere y **sube su comprobante (PDF o imagen) antes de confirmar el pedido**; el administrador lo revisa y confirma o rechaza el pago. El stock se aparta al confirmar el pedido (no al agregar al carrito) y se devuelve si el pedido se cancela. Los reportes cuentan solo pedidos con pago confirmado.
- Historial de pedidos del cliente y seguimiento del estado desde administración.
- Panel de estadísticas de ventas, ranking, inventario y clientes.
- Asistente de consultas sobre SQLite **sin IA generativa ni API de pago**. Reconoce categorías de preguntas y ejecuta consultas SQL predefinidas, no genera SQL arbitrario.
- Administración de perfumes con imágenes, precios y existencias.

## Notas
- Los datos bancarios para transferencias están en `CUENTA_BANCARIA` (app.py): **cámbialos por los reales**. Los comprobantes se guardan en la carpeta privada `comprobantes/` (no en `static`).
- La base `tienda.db` incluida procede del proyecto original y se actualiza automáticamente agregando el campo de estado de pedido.
- Las gráficas se dibujan con Canvas nativo de JavaScript y funcionan sin conexión. Las tipografías tienen fuentes de respaldo locales.
- Los perfumes sin imagen usan una ilustración CSS. Puedes cargar fotografías desde administración.
- La clave de sesión se genera al iniciar si no defines `FRAG_SECRET_KEY` en el entorno; las sesiones se invalidan tras reiniciar en ese caso.
- Para producción serían necesarias protecciones adicionales (CSRF, control de despliegue, credenciales seguras, etc.).
