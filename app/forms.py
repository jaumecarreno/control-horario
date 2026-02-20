"""WTForms form classes."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from flask_wtf import FlaskForm
from flask_wtf.file import FileField
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Email, InputRequired, Length, NumberRange, Optional, ValidationError


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)], filters=[lambda value: value.strip() if value else value])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=8, max=255)])
    remember = BooleanField("Remember me")
    submit = SubmitField("Sign in")


class TenantSelectForm(FlaskForm):
    tenant_id = SelectField("Tenant", choices=[], validators=[DataRequired()], coerce=str)
    submit = SubmitField("Use tenant")


class PasswordChangeForm(FlaskForm):
    current_password = PasswordField("Contraseña actual", validators=[DataRequired(), Length(min=8, max=255)])
    new_password = PasswordField("Nueva contraseña", validators=[DataRequired(), Length(min=8, max=255)])
    confirm_password = PasswordField("Confirmar nueva contraseña", validators=[DataRequired(), Length(min=8, max=255)])
    submit = SubmitField("Actualizar contraseña")


class AdminResetPasswordForm(FlaskForm):
    temporary_password = PasswordField("Contraseña temporal", validators=[DataRequired(), Length(min=8, max=255)])
    submit = SubmitField("Resetear contraseña")


class EmployeeCreateForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=255)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=255)])
    pin = PasswordField("PIN", validators=[Optional(), Length(min=4, max=16)])
    active = BooleanField("Active", default=True)
    submit = SubmitField("Create employee")


class EmployeeEditForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=255)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=255)])
    pin = PasswordField("New PIN (optional)", validators=[Optional(), Length(min=4, max=16)])
    active = BooleanField("Active", default=True)
    assignment_shift_id = SelectField("Asignar turno", choices=[], validators=[Optional()], default="", coerce=str)
    punch_approver_user_id = SelectField("Aprobador de rectificaciones", choices=[], validators=[Optional()], default="", coerce=str)
    assignment_effective_from = DateField("Aplicar desde", validators=[Optional()], default=date.today)
    submit = SubmitField("Guardar cambios")


class UserCreateForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)], filters=[lambda value: value.strip() if value else value])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=8, max=255)])
    confirm_password = PasswordField("Confirm password", validators=[DataRequired(), Length(min=8, max=255)])
    role = SelectField(
        "Role",
        choices=[
            ("OWNER", "Owner"),
            ("ADMIN", "Admin"),
            ("MANAGER", "Manager"),
            ("EMPLOYEE", "Employee"),
        ],
        validators=[DataRequired()],
        default="EMPLOYEE",
    )
    employee_id = SelectField("Employee", choices=[], validators=[Optional()], coerce=str)
    active = BooleanField("Active", default=True)
    submit = SubmitField("Create user")


class UserEditForm(FlaskForm):
    role = SelectField(
        "Role",
        choices=[
            ("OWNER", "Owner"),
            ("ADMIN", "Admin"),
            ("MANAGER", "Manager"),
            ("EMPLOYEE", "Employee"),
        ],
        validators=[DataRequired()],
    )
    employee_id = SelectField("Employee", choices=[], validators=[Optional()], coerce=str)
    active = BooleanField("Active", default=True)
    submit = SubmitField("Guardar cambios")

    def validate_role(self, field: SelectField) -> None:
        allowed_roles = {"OWNER", "ADMIN", "MANAGER", "EMPLOYEE"}
        if field.data not in allowed_roles:
            raise ValidationError("Rol invalido.")

    def validate_employee_id(self, field: SelectField) -> None:
        role = self.role.data
        employee_id = (field.data or "").strip()
        if role == "EMPLOYEE":
            if not employee_id:
                raise ValidationError("Debe seleccionar un empleado para el rol EMPLOYEE.")
            try:
                UUID(employee_id)
            except ValueError as exc:
                raise ValidationError("Empleado invalido para el tenant actual.") from exc
            return

        if employee_id:
            raise ValidationError("Los roles admin/manager/owner no deben tener empleado asociado.")


class LeaveRequestForm(FlaskForm):
    type_id = SelectField("Vacacion / permiso", choices=[], validators=[DataRequired()], coerce=str)
    date_from = DateField("Desde", validators=[DataRequired()])
    date_to = DateField("Hasta", validators=[DataRequired()])
    reason = TextAreaField("Motivo", validators=[DataRequired(), Length(min=10, max=500)])
    attachment = FileField("Adjunto (opcional)")
    minutes = IntegerField("Minutos (opcional)", validators=[Optional()])
    submit = SubmitField("Enviar solicitud")

    def validate_date_to(self, field: DateField) -> None:
        if self.date_from.data and field.data and field.data < self.date_from.data:
            raise ValidationError("La fecha de fin debe ser igual o posterior a la fecha de inicio.")


class PunchCorrectionRequestForm(FlaskForm):
    source_event_id = StringField("Fichaje a rectificar", validators=[DataRequired(), Length(max=64)])
    requested_date = DateField("Nueva fecha", validators=[DataRequired()])
    requested_hour = IntegerField("Nueva hora", validators=[InputRequired(), NumberRange(min=0, max=23)])
    requested_minute = IntegerField("Nuevos minutos", validators=[InputRequired(), NumberRange(min=0, max=59)])
    requested_kind = SelectField(
        "Nuevo tipo",
        choices=[("IN", "Entrada"), ("OUT", "Salida")],
        validators=[DataRequired()],
    )
    reason = TextAreaField("Motivo", validators=[DataRequired(), Length(min=10, max=300)])
    attachment = FileField("Adjunto (opcional)")
    submit = SubmitField("Enviar solicitud")

    def validate_source_event_id(self, field: StringField) -> None:
        try:
            UUID((field.data or "").strip())
        except ValueError as exc:
            raise ValidationError("Fichaje a rectificar invalido.") from exc


class DateRangeExportForm(FlaskForm):
    date_from = DateField("From", validators=[DataRequired()], default=date.today)
    date_to = DateField("To", validators=[DataRequired()], default=date.today)
    submit = SubmitField("Export CSV")

    def validate_date_to(self, field: DateField) -> None:
        if self.date_from.data and field.data and field.data < self.date_from.data:
            raise ValidationError("End date must be on or after start date.")


class AttendanceReportForm(FlaskForm):
    report_type = SelectField(
        "Tipo de reporte",
        choices=[
            ("control", "Control horario"),
            ("executive", "Resumen ejecutivo"),
        ],
        validators=[DataRequired()],
        default="control",
    )
    output_format = SelectField(
        "Formato",
        choices=[
            ("csv", "CSV"),
            ("json", "JSON"),
            ("xlsx", "XLSX"),
            ("pdf", "PDF"),
        ],
        validators=[DataRequired()],
        default="csv",
    )
    employee_id = SelectField("Empleado (opcional)", choices=[], validators=[Optional()], coerce=str)
    date_from = DateField("Desde", validators=[DataRequired()], default=date.today)
    date_to = DateField("Hasta", validators=[DataRequired()], default=date.today)
    submit = SubmitField("Generar reporte")

    def validate_date_to(self, field: DateField) -> None:
        if self.date_from.data and field.data and field.data < self.date_from.data:
            raise ValidationError("La fecha fin debe ser igual o posterior a la fecha inicio.")

    def validate_employee_id(self, field: SelectField) -> None:
        employee_id = (field.data or "").strip()
        if not employee_id:
            return
        try:
            UUID(employee_id)
        except ValueError as exc:
            raise ValidationError("Empleado invalido.") from exc


class ShiftCreateForm(FlaskForm):
    name = StringField("Nombre", validators=[DataRequired(), Length(max=128)])
    break_counts_as_worked_bool = BooleanField("El descanso cuenta como jornada laboral", default=True)
    break_minutes = IntegerField("Minutos de descanso", validators=[DataRequired(), NumberRange(min=0, max=1440)], default=30)
    expected_hours = DecimalField("Horas trabajadas", validators=[DataRequired(), NumberRange(min=0, max=9999)], places=2)
    expected_hours_frequency = SelectField(
        "Frecuencia",
        choices=[
            ("YEARLY", "Anuales"),
            ("MONTHLY", "Mensuales"),
            ("WEEKLY", "Semanales"),
            ("DAILY", "Diarias"),
        ],
        validators=[DataRequired()],
        default="DAILY",
    )
    submit = SubmitField("Crear turno")


class BulkEmployeeImportForm(FlaskForm):
    csv_file = FileField("CSV de empleados", validators=[DataRequired()])
    submit = SubmitField("Validar CSV")


class BulkImportCommitForm(FlaskForm):
    import_job_id = StringField("Import job id", validators=[DataRequired(), Length(max=64)])
    submit = SubmitField("Confirmar importacion")

    def validate_import_job_id(self, field: StringField) -> None:
        try:
            UUID((field.data or "").strip())
        except ValueError as exc:
            raise ValidationError("Import job invalido.") from exc
