"""Invoices and before/after photos attached when work is done (projects, repairs, routine visits)."""
from flask import request
from flask_login import current_user

from . import files
from .extensions import db
from .models import WorkFile

INPUTS = (("invoice_files", "invoice"), ("before_files", "before"), ("after_files", "after"))


def take_all(errors, **links):
    """Store every file sent in the invoice / before / after inputs, linked to the given request, routine
    visit and/or cost. Returns how many were added (the caller commits)."""
    added = 0
    for field, kind in INPUTS:
        for upload in request.files.getlist(field):
            if not upload or not upload.filename:
                continue
            stored = files.take_upload(upload, errors, required=False)
            if stored is not None:
                db.session.add(WorkFile(kind=kind, file=stored, uploaded_by_id=current_user.id, **links))
                added += 1
    return added
