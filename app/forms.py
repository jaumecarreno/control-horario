"""WTForms form classes."""

from __future__ import annotations

from datetime import date

from flask_wtf import FlaskForm
from wtforms import BooleanField, DateField, IntegerField, PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, Length, Optional, ValidationError


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
    shift_id = SelectField("Shift", choices=[], validators=[Optional()], coerce=str)
    active = BooleanField("Active", default=True)
    submit = SubmitField("Create employee")


class EmployeeEditForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=255)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=255)])
    pin = PasswordField("PIN", validators=[Optional(), Length(min=4, max=16)])
    shift_id = SelectField("Shift", choices=[], validators=[Optional()], coerce=str)
    submit = SubmitField("Save changes")


class ShiftForm(FlaskForm):
    name = StringField("Shift name", validators=[DataRequired(), Length(max=255)])
    break_counts_as_work = BooleanField("Break counts as work time", default=True)
    break_minutes = IntegerField("Break minutes", validators=[DataRequired()])
    expected_hours = IntegerField("Expected hours", validators=[DataRequired()])
    expected_hours_period = SelectField(
        "Period",
        choices=[("ANNUAL", "Anuales"), ("MONTHLY", "Mensuales"), ("WEEKLY", "Semanales"), ("DAILY", "Diarias")],
        validators=[DataRequired()],
        coerce=str,
    )
    submit = SubmitField("Save shift")


class LeaveRequestForm(FlaskForm):
    type_id = SelectField("Leave type", choices=[], validators=[DataRequired()], coerce=str)
    date_from = DateField("From", validators=[DataRequired()])
    date_to = DateField("To", validators=[DataRequired()])
    minutes = IntegerField("Minutes (optional)", validators=[Optional()])
    submit = SubmitField("Submit request")

    def validate_date_to(self, field: DateField) -> None:
        if self.date_from.data and field.data and field.data < self.date_from.data:
            raise ValidationError("End date must be on or after start date.")


class DateRangeExportForm(FlaskForm):
    date_from = DateField("From", validators=[DataRequired()], default=date.today)
    date_to = DateField("To", validators=[DataRequired()], default=date.today)
    submit = SubmitField("Export CSV")

    def validate_date_to(self, field: DateField) -> None:
        if self.date_from.data and field.data and field.data < self.date_from.data:
            raise ValidationError("End date must be on or after start date.")
