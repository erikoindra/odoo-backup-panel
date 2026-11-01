import re

from django import forms

from panel import dockerops

DB_NAME = re.compile(r"[a-z0-9_]{1,63}")
RESERVED = {"postgres", "template0", "template1"}


class DeleteForm(forms.Form):
    """The confirmation of a delete: the word DELETE, because the files themselves are listed above it."""

    confirm = forms.CharField(label="Type DELETE to confirm", strip=True, required=False)

    def clean_confirm(self):
        if self.cleaned_data["confirm"] != "DELETE":
            raise forms.ValidationError("Type DELETE in capital letters to continue.")
        return self.cleaned_data["confirm"]


class RestoreForm(forms.Form):
    filename = forms.CharField(label="Backup file", max_length=255, strip=True)
    new_db = forms.CharField(label="New database name", max_length=63, strip=True)
    neutralize = forms.BooleanField(label="Neutralize database", required=False)
    confirm = forms.CharField(label="Type the database name to confirm", strip=True)

    def clean_filename(self):
        name = self.cleaned_data["filename"]
        if not dockerops.ZIP_NAME.fullmatch(name):
            raise forms.ValidationError("Choose one of the backup files.")
        return name

    def clean_new_db(self):
        name = self.cleaned_data["new_db"]
        if not DB_NAME.fullmatch(name):
            raise forms.ValidationError("Only lowercase letters, digits and underscores (max 63).")
        if name in RESERVED:
            raise forms.ValidationError("PostgreSQL uses that name. Choose another.")
        return name

    def clean(self):
        data = super().clean()
        if data.get("new_db") and data.get("confirm") != data["new_db"]:
            self.add_error("confirm", "Type the database name exactly to continue.")
        return data
