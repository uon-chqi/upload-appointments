from django import forms

from .models import Facility


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
