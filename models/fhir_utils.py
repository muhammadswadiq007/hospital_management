"""Small helpers shared by the models' to_fhir() methods."""


def fhir_datetime(value):
    """Odoo stores datetimes in UTC without a timezone; FHIR needs one. 'Z' = UTC."""
    return value.strftime('%Y-%m-%dT%H:%M:%SZ') if value else None


def fhir_ref(resource_type, record):
    """A FHIR Reference, e.g. {'reference': 'Patient/7', 'display': 'Jane Doe'}."""
    if not record:
        return None
    return {'reference': f'{resource_type}/{record.id}', 'display': record.display_name}


def clean(resource):
    """Drop empty values: FHIR does not allow null or empty elements."""
    return {k: v for k, v in resource.items() if v not in (None, False, '', [], {})}
