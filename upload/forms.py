from django import forms

from .models import Facility, TenantServer


class UploadForm(forms.Form):
    date_from = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
        label='From Date',
    )
    date_to = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
        label='To Date',
    )

    def clean(self):
        cleaned_data = super().clean()
        date_from = cleaned_data.get('date_from')
        date_to = cleaned_data.get('date_to')
        if date_from and date_to and date_from > date_to:
            raise forms.ValidationError("'From Date' must be before or equal to 'To Date'.")
        return cleaned_data


class FacilityForm(forms.ModelForm):
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={'class': 'form-control'}),
        help_text='Leave blank to keep the existing password.',
    )

    class Meta:
        model = Facility
        fields = ['name', 'host', 'port', 'database_name', 'username', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'host': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'container name, hostname, or IP',
            }),
            'port': forms.NumberInput(attrs={'class': 'form-control'}),
            'database_name': forms.TextInput(attrs={'class': 'form-control'}),
            'username': forms.TextInput(attrs={'class': 'form-control'}),
        }

    def clean_password(self):
        password = self.cleaned_data.get('password', '')
        # Only a brand-new facility must supply one; editing without retyping the
        # password keeps whatever is already encrypted on the row.
        if not password and self.instance.pk is None:
            raise forms.ValidationError('A MySQL password is required.')
        return password

    def save(self, commit=True):
        facility = super().save(commit=False)
        password = self.cleaned_data.get('password')
        if password:
            facility.set_password(password)
        if commit:
            facility.save()
        return facility


class TenantServerForm(forms.ModelForm):
    """One MySQL instance's connection, plus which of its schemas count.

    Deliberately has no per-facility fields: which facilities exist is the
    server's answer to give, not the operator's to type.
    """

    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={'class': 'form-control'}),
        help_text='Leave blank to keep the existing password.',
    )

    class Meta:
        model = TenantServer
        fields = ['name', 'host', 'port', 'username', 'database_prefix', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'host': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'hostname or IP of the cloud MySQL',
            }),
            'port': forms.NumberInput(attrs={'class': 'form-control'}),
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'database_prefix': forms.TextInput(attrs={'class': 'form-control'}),
        }

    def clean_password(self):
        password = self.cleaned_data.get('password', '')
        # Same rule as a facility: only a brand-new server must supply one.
        if not password and self.instance.pk is None:
            raise forms.ValidationError('A MySQL password is required.')
        return password

    def clean_database_prefix(self):
        # Blank is allowed and means "every database this account can see", which
        # is a legitimate setup for a login scoped to the tenant schemas. Only
        # surrounding whitespace is a typo worth silently fixing.
        return self.cleaned_data.get('database_prefix', '').strip()

    def save(self, commit=True):
        server = super().save(commit=False)
        password = self.cleaned_data.get('password')
        if password:
            server.set_password(password)
        if commit:
            server.save()
        return server
