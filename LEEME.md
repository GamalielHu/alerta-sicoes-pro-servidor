# Alerta SICOES Pro: servidor y panel

- `servidor/vigilar.py`: revisa el SICOES cada 15 min (GitHub Actions + cron-job.org) y avisa por Firebase solo a las cuentas activas, cada una con sus entidades.
- `panel/`: panel web del administrador (GitHub Pages): crear cuentas, habilitar/deshabilitar, fechas, liberar celular.
- `firestore.rules`: reglas de seguridad de la base de datos.
- `servidor/configurar.py`: deja listo el proyecto de Firebase y genera `panel/config.js`.

Secreto necesario: `FIREBASE_CUENTA` (clave privada de la cuenta de servicio del proyecto alerta-sicoes-pro).
La app Android está en el repo privado `alerta-sicoes-pro`.
