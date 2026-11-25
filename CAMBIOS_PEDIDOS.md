# Cambios implementados en pedidos FRAG-IA

Se ajustó el flujo de pedidos sin cambiar la lógica de ventas y reportes existente.

## Flujo
- El administrador mantiene el control de `En proceso`, `En camino` y `Entregado`.
- El cliente no marca `Entregado`; únicamente puede confirmar que lo recibió.
- El cliente puede reportar una incidencia desde un formulario sencillo.
- Un pedido en `Entregado` se cierra automáticamente como `Recibido` después de 5 días sin incidencia.
- El administrador puede resolver una incidencia y cerrar el pedido como `Recibido`.

## Trazabilidad
Se agregó la tabla `pedido_historial` para guardar estado y fecha/hora de cada cambio.
También se agregaron fechas de envío, entrega, recepción e incidencia en `ventas`.

## Reportes
`Con incidencia` se considera un pedido ya pagado, por lo que no se descuenta de las ventas e ingresos de los reportes.

## Interfaz
- `templates/mis_pedidos.html`: vista de cliente con tarjetas, estados, historial, confirmación y modal de incidencia.
- `templates/admin_pedidos.html`: panel de administración con resumen, gestión del estado, incidencias e historial.
