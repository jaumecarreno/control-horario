"""WTForms form classes."""

from __future__ import annotations

from datetime import date

from flask_wtf import FlaskForm
from wtforms import BooleanField, DateField, DecimalField, IntegerField, PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, Length, NumberRange, Optional, ValidationError


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=8, max=255)])
    remember = BooleanField("Remember me")
    submit = SubmitField("Sign in")


class TenantSelectForm(FlaskForm):
    tenant_id = SelectField("Tenant", choices=[], validators=[DataRequired()], coerce=str)
    submit = SubmitField("Use tenant")


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
    assignment_effective_from = DateField("Aplicar desde", validators=[Optional()], default=date.today)
    submit = SubmitField("Guardar cambios")


class LeaveRequestForm(FlaskForm):
    type_id = SelectField("Vacacion / permiso", choices=[], validators=[DataRequired()], coerce=str)
    date_from = DateField("Desde", validators=[DataRequired()])
    date_to = DateField("Hasta", validators=[DataRequired()])
    minutes = IntegerField("Minutos (opcional)", validators=[Optional()])
    submit = SubmitField("Enviar solicitud")

    def validate_date_to(self, field: DateField) -> None:
        if self.date_from.data and field.data and field.data < self.date_from.data:
            raise ValidationError("La fecha de fin debe ser igual o posterior a la fecha de inicio.")


class DateRangeExportForm(FlaskForm):
    date_from = DateField("From", validators=[DataRequired()], default=date.today)
    date_to = DateField("To", validators=[DataRequired()], default=date.today)
    submit = SubmitField("Export CSV")

    def validate_date_to(self, field: DateField) -> None:
        if self.date_from.data and field.data and field.data < self.date_from.data:
            raise ValidationError("End date must be on or after start date.")


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
