# Especificacion MVP de Permisos y Bolsas

## Objetivo
Definir un flujo operativo, multi-tenant y compatible hacia atras para solicitudes de vacaciones/permisos, con reglas claras de validacion, saldo, aprobacion y auditoria.

## Alcance
- Flujo de estados: `REQUESTED -> APPROVED | REJECTED | CANCELLED`.
- Reglas de consumo por unidad (`DAYS`, `HOURS`).
- Validaciones de saldo, rango de fechas y solape.
- Control de permisos por rol y por tenant activo.
- Auditoria de cada transicion de estado.

## Fuera de alcance
- Prorrateo anual avanzado.
- Arrastre de saldo entre anos.
- Reglas legales de un pais especifico.

## Contrato funcional

### Empleado: `POST /me/leaves`
- Debe aceptar solicitudes solo para politicas activas del turno actual.
- Debe validar:
  - Rango de fechas dentro de la vigencia de la politica.
  - Para `HOURS`, `minutes > 0`.
  - Saldo disponible incluyendo solicitudes pendientes.
  - Ausencia de solape con solicitudes activas (`REQUESTED` o `APPROVED`) para la misma politica.
- Debe crear la solicitud con estado `REQUESTED`.

### Empleado: `POST /me/leaves/<id>/cancel`
- Solo puede cancelar solicitudes propias.
- Solo puede cancelar solicitudes en estado `REQUESTED`.
- Si el estado no es `REQUESTED`, responde con conflicto (`409`).
- Cambia estado a `CANCELLED` y registra auditoria.

### Admin/Manager/Owner: `POST /admin/approvals/<id>/approve|reject`
- Solo puede decidir solicitudes del tenant activo.
- Si la solicitud no esta en `REQUESTED`, responde con conflicto (`409`).
- Cambia estado a `APPROVED` o `REJECTED`, guarda `approver_user_id` y `decided_at`, y registra auditoria.

## Reglas de calculo
- `DAYS`: importe solicitado = `date_to - date_from + 1`.
- `HOURS`: importe solicitado = `minutes / 60`.
- Saldo consumido = `approved + pending`.
- Restriccion: `used + requested <= policy.amount`.

## Auditoria minima por evento
Acciones:
- `LEAVE_REQUESTED`
- `LEAVE_APPROVED`
- `LEAVE_REJECTED`
- `LEAVE_CANCELLED`

Payload minimo:
- `employee_id`
- `type_id`
- `leave_policy_id` (nullable)
- `date_from`
- `date_to`
- `minutes`
- `status`

## Criterios de aceptacion
- No se permiten solapes activos por politica.
- No se permite consumir mas saldo del disponible considerando pendientes.
- Cancelacion solo en `REQUESTED`.
- Decision administrativa idempotente de negocio (segunda decision sobre solicitud ya decidida falla con `409`).
- Todos los cambios de estado generan registro en `audit_log`.
