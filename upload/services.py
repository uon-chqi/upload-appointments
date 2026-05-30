import json
import logging
import time
import traceback

import requests
from django.conf import settings
from django.db import connections

logger = logging.getLogger(__name__)

APPOINTMENT_QUERY = """
with vls as(SELECT distinct
    e.patient_id,
    CASE 
        WHEN cn2.name = 'NOT DETECTED' THEN '0.00' 
        ELSE (
            CASE 
                WHEN o.concept_id IN (5497,730,654,790,856) THEN o.value_numeric
                WHEN o.concept_id IN (1030,1305) THEN o.value_coded
            END
        )
    END AS TestResults,
    o.obs_datetime AS VLDate
FROM openmrs.encounter e
INNER JOIN openmrs.obs o 
    ON e.encounter_id = o.encounter_id 
    AND o.voided = 0
    AND o.concept_id IN (5497,730,654,790,856,1030,1305)
LEFT JOIN openmrs.orders od 
    ON od.order_id = o.order_id 
    AND od.voided = 0
LEFT JOIN openmrs.concept_name cn1 
    ON cn1.concept_id = o.concept_id
    AND cn1.locale = 'en' 
    AND cn1.concept_name_type = 'FULLY_SPECIFIED'
LEFT JOIN openmrs.concept_name cn2 
    ON cn2.concept_id = (
        CASE 
            WHEN o.concept_id IN (1030,1305) THEN o.value_coded
        END
    )
    AND cn2.locale = 'en' 
    AND cn2.concept_name_type = 'FULLY_SPECIFIED'
WHERE e.voided = 0 AND cn1.name LIKE '%viral%'),
cd4 as (SELECT e.patient_id,
    o.value_numeric AS TestResults,
    o.obs_datetime AS TestDate
FROM openmrs.encounter e
INNER JOIN openmrs.obs o 
    ON e.encounter_id = o.encounter_id 
    AND o.voided = 0
    AND o.concept_id = 5497
LEFT JOIN openmrs.orders od 
    ON od.order_id = o.order_id 
    AND od.voided = 0
LEFT JOIN openmrs.concept_name cn1 
    ON cn1.concept_id = o.concept_id
    AND cn1.locale = 'en' 
    AND cn1.concept_name_type = 'FULLY_SPECIFIED'
WHERE e.voided = 0 AND cn1.name LIKE '%cd4%')
select max(a.patient_id) as patient_id
, max(a.visit_date) as visit_date 
, max(appointment_date) as appointment_date
, max(patient_name) as patient_name
, max(a.gender) as gender
, max(a.dob) as dob
, max(a.age) as age
, max(a.ccc_number) as ccc_number
, max(a.phone_number) as phone_number
, max(a.risk_classification) as risk_classification_value
, coalesce(max(a.risk_score), (case when max(a.risk_classification) is null then 'Unknown'
	when max(a.risk_classification) = 0 then 'Unknown'
	when max(a.risk_classification) <= (select property_value from global_property where property='kenyaemrml.iit.lowRiskThreshold' limit 1) then 'Low Risk'
	when max(a.risk_classification) <= (select property_value from global_property where property='kenyaemrml.iit.mediumRiskThreshold' limit 1) then 'Medium Risk'
	when max(a.risk_classification) <= (select property_value from global_property where property='kenyaemrml.iit.highRiskThreshold' limit 1) then 'High Risk'
	else 'Unknown'
	end)) as risk_classification
, max(a.risk_factors) as risk_factors
, (select TestResults from vls x where x.patient_id=a.patient_id order by VLDate desc limit 1) as last_viral_load
, (select VLDate from vls x where x.patient_id=a.patient_id order by VLDate desc limit 1) as last_viral_load_date
, (select TestResults from cd4 x where x.patient_id=a.patient_id order by TestDate desc limit 1) as last_cd4
, (select TestDate from cd4 x where x.patient_id=a.patient_id order by TestDate desc limit 1) as last_cd4_date
, max(a.appointment_status) as appointment_status
, max(a.county) as county
, max(a.sub_county) as sub_county
, max(a.ward) as ward
, max(a.city_village) as city_village
, max(a.landmark) as landmark
, max(a.address5) as address5
, max(a.address6) as address6
, max(a.marital_status) as marital_status
, max(a.case_manager_assigned) as case_manager_assigned
, max(a.facility_mfl) as facility_mfl
, max(a.facility_name) as facility_name
, max(a.consented) as consented
, max(a.program) as program
, max(a.lmp) as lmp
, max(a.edd) as edd
, max(a.delivery_date) as delivery_date
, max(a.intervention_list) as intervention_list
, max(a.cormobidities) as cormobidities
, max(a.enrollment_date) as enrollment_date
, max(a.start_ART_date) as start_ART_date
, case when max(a.restart_ART_date) > max(a.start_ART_date) then max(a.restart_ART_date) else null end as restart_ART_date
from
(
    select distinct a.patient_id
    , date(a.date_started) as visit_date
    , (select x.start_date_time from openmrs.patient_appointment x where x.patient_id = a.patient_id and x.status != 'Cancelled' and x.voided=0
    order by x.start_date_time desc limit 1) as appointment_date
    , (select concat(x.given_name, ' ', ifnull(x.middle_name, '') , ' ', x.family_name) 
        from openmrs.person_name x where x.person_id =a.patient_id limit 1) as patient_name
    , b.gender
    , b.birthdate as dob
    , round(TIMESTAMPDIFF(month, b.birthdate, now())/12.0, 0) as age
    , (select x.identifier from openmrs.patient_identifier x inner join openmrs.patient_identifier_type y on x.identifier_type=y.patient_identifier_type_id
    and y.name ='Unique Patient Number' where x.patient_id = a.patient_id limit 1) as ccc_number
    , (select x.value from openmrs.person_attribute x inner join openmrs.person_attribute_type y on x.person_attribute_type_id =y.person_attribute_type_id
    and y.name='Telephone contact' where x.person_id = a.patient_id limit 1) as phone_number
    , (select x.value_numeric from obs x where x.concept_id=167162 and x.person_id=a.patient_id order by x.obs_datetime desc limit 1) as risk_classification
    , 'Pending' as appointment_status
    , c.name
    , d.county_district as county
    , d.state_province as sub_county
    , d.address4 as ward
    , d.city_village
    , d.address2 as landmark 
    , d.address5
    , d.address6 
    , (select y.name from obs x inner join concept_name y on x.value_coded=y.concept_id where x.concept_id =1054 and y.locale ='en' and x.person_id=a.patient_id limit 1) as marital_status
    , null as case_manager_assigned
    , (select x.value_reference from openmrs.location_attribute x where x.location_id = a.location_id) as facility_mfl
    , (select x.name from openmrs.location x where x.location_id = a.location_id) as facility_name
    , (select case when x.value_coded=1065 then 'Yes' else 'No' end as response from obs x where x.concept_id=166607 and x.person_id=a.patient_id order by obs_datetime desc limit 1) as consented
    , (SELECT GROUP_CONCAT(y.name SEPARATOR ', ') FROM patient_program x INNER JOIN program y ON x.program_id = y.program_id and x.patient_id=a.patient_id WHERE x.date_completed IS NULL) AS program
    , (select risk_score from kenyaemr_ml_patient_risk_score x where x.patient_id = a.patient_id order by x.evaluation_date desc limit 1) as risk_score
    , (select risk_factors from kenyaemr_ml_patient_risk_score x where x.patient_id = a.patient_id order by x.evaluation_date desc limit 1) as risk_factors
	, null as lmp
	, null as edd
	, null as delivery_date
	, null as intervention_list
	, null as cormobidities
	, (select x.date_enrolled from patient_program x inner join program y on x.program_id =y.program_id where y.name ='HIV' and x.patient_id =a.patient_id limit 1) as enrollment_date
	, (select x.date_enrolled from patient_program x inner join program y on x.program_id =y.program_id where y.name ='HIV' and x.patient_id =a.patient_id limit 1) as start_ART_date
	, (select x.date_enrolled from patient_program x inner join program y on x.program_id =y.program_id where y.name ='HIV' and x.patient_id =a.patient_id order by x.date_enrolled desc limit 1) as restart_ART_date
    from openmrs.visit a 
    inner join openmrs.person b on a.patient_id = b.person_id
    left join openmrs.visit_type c on a.visit_type_id = c.visit_type_id
    left join person_address d on a.patient_id = d.person_id
    where a.date_started >= %s and a.date_started <= %s
    and a.patient_id in (select x.patient_id from patient_program x
						inner join program y on x.program_id =y.program_id 
						where y.name ='HIV')
) a where a.consented='Yes' group by a.patient_id
"""


def fetch_appointments(date_from, date_to):
    """Query OpenMRS database for appointment data within a date range."""
    connection = connections['openmrs']
    with connection.cursor() as cursor:
        cursor.execute(APPOINTMENT_QUERY, [
            date_from.strftime('%Y-%m-%d'),
            date_to.strftime('%Y-%m-%d'),
        ])
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()

    def safe_str(value, default=''):
        """Convert a value to string, returning default for None/empty."""
        if value is None:
            return default
        return str(value).strip() or default

    def safe_int(value, default=0):
        """Convert a value to int, returning default on failure."""
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def safe_float(value, default=0.0):
        """Convert a value to float, returning default on failure."""
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    patients = []
    for row in rows:
        record = dict(zip(columns, row))
        patients.append({
            'patient_id': safe_str(record.get('patient_id')),
            'patient_name': safe_str(record.get('patient_name')),
            'gender': safe_str(record.get('gender')),
            'dob': safe_str(record.get('dob')),
            'age': safe_int(record.get('age')),
            'ccc_number': safe_str(record.get('ccc_number')),
            'phone_number': safe_str(record.get('phone_number')),
            'email': '',
            'physical_address': safe_str(record.get('city_village')),
            'enrollment_date': safe_str(record.get('enrollment_date')),
            'county': safe_str(record.get('county')),
            'sub_county': safe_str(record.get('sub_county')),
            'ward': safe_str(record.get('ward')),
            'city_village': safe_str(record.get('city_village')),
            'landmark': safe_str(record.get('landmark')),
            'address5': safe_str(record.get('address5')),
            'address6': safe_str(record.get('address6')),
            'marital_status': safe_str(record.get('marital_status')),
            'facility_mfl': safe_str(record.get('facility_mfl')),
            'facility_name': safe_str(record.get('facility_name')),
            'risk_classification': safe_str(record.get('risk_classification'), 'Unknown'),
            'risk_classification_value': safe_float(record.get('risk_classification_value')),
            'risk_factors': safe_str(record.get('risk_factors')),
            'last_viral_load': safe_str(record.get('last_viral_load')),
            'last_viral_load_date': safe_str(record.get('last_viral_load_date')),
            'last_cd4': safe_str(record.get('last_cd4')),
            'last_cd4_date': safe_str(record.get('last_cd4_date')),
            'appointment_status': safe_str(record.get('appointment_status'), 'Pending'),
            'appointment_date': safe_str(record.get('appointment_date')),
            'visit_date': safe_str(record.get('visit_date')),
            'case_manager_assigned': safe_str(record.get('case_manager_assigned')),
            'program': safe_str(record.get('program')),
            'lmp': safe_str(record.get('lmp')),
            'edd': safe_str(record.get('edd')),
            'delivery_date': safe_str(record.get('delivery_date')),
            'intervention_list': safe_str(record.get('intervention_list')),
            'cormobidities': safe_str(record.get('cormobidities')),
            'start_ART_date': safe_str(record.get('start_ART_date')),
            'restart_ART_date': safe_str(record.get('restart_ART_date')),
        })
    return patients


def get_api_token():
    """Authenticate with the Ushauri DIFF platform and return a bearer token."""
    url = f"{settings.CHQI_API_BASE_URL}/api/auth/login"
    username = settings.CHQI_API_USERNAME
    password = settings.CHQI_API_PASSWORD
    if not username or not password:
        raise ValueError(
            f"Ushauri DIFF platform credentials not configured "
            f"(username={'set' if username else 'MISSING'}, "
            f"password={'set' if password else 'MISSING'})"
        )
    max_retries = 3
    retry_delay = 10  # seconds
    for attempt in range(1, max_retries + 1):
        response = requests.post(
            url,
            data=json.dumps({
                'username': username,
                'password': password,
            }),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        if response.ok:
            break
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        logger.error("Login attempt %d/%d failed (HTTP %s): %s",
                      attempt, max_retries, response.status_code, detail)
        # Retry on server errors (5xx), not client errors (4xx)
        if response.status_code >= 500 and attempt < max_retries:
            time.sleep(retry_delay * attempt)
            continue
        response.raise_for_status()
    data = response.json()
    logger.info("Login response keys: %s", list(data.keys()) if isinstance(data, dict) else data)
    # Try common token field names; search nested structures too
    token = (
        data.get('token')
        or data.get('access_token')
        or data.get('accessToken')
        or (data.get('data', {}) or {}).get('token')
        or (data.get('data', {}) or {}).get('access_token')
        or (data.get('data', {}) or {}).get('accessToken')
    )
    if not token:
        raise ValueError(f"Could not find token in login response: {data}")
    return token


def upload_patients(patients, token, log=None, batch_size=10):
    """POST patient data to the Ushauri DIFF platform in batches."""
    import math
    url = f"{settings.CHQI_API_BASE_URL}/api/patients/upload-json"
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }
    total_batches = math.ceil(len(patients) / batch_size)
    if log:
        log.batches_total = total_batches
        log.batches_completed = 0
        log.save(update_fields=['batches_total', 'batches_completed'])

    results = []
    for i in range(0, len(patients), batch_size):
        batch = patients[i:i + batch_size]
        batch_num = i // batch_size + 1
        logger.info("Uploading batch %d/%d (%d–%d of %d patients)",
                     batch_num, total_batches, i + 1, i + len(batch), len(patients))
        response = requests.post(
            url,
            data=json.dumps({'patients': batch}),
            headers=headers,
            timeout=1000,
        )
        if not response.ok:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise requests.HTTPError(
                f"HTTP {response.status_code}: {detail}",
                response=response,
            )
        results.append(response.json())
        if log:
            log.batches_completed = batch_num
            log.save(update_fields=['batches_completed'])
    return results


def run_upload(date_from, date_to, triggered_by='manual', user=None, log=None):
    """Full pipeline: query OpenMRS, authenticate, upload, and log the result."""
    from .models import UploadLog

    if log is None:
        log = UploadLog(
            date_from=date_from,
            date_to=date_to,
            triggered_by=triggered_by,
            triggered_by_user=user,
        )
    log.status = 'in_progress'
    log.save()

    try:
        patients = fetch_appointments(date_from, date_to)
        log.records_uploaded = len(patients)
        log.save(update_fields=['records_uploaded'])

        if not patients:
            log.status = 'success'
            log.error_message = 'No records found for the given period.'
            log.save(update_fields=['status', 'error_message'])
            return log

        token = get_api_token()
        upload_patients(patients, token, log=log)

        log.status = 'success'
        log.save(update_fields=['status'])
        logger.info(
            "Upload successful: %d records for %s to %s",
            len(patients), date_from, date_to,
        )
    except Exception:
        log.error_message = traceback.format_exc()
        log.status = 'failed'
        log.save(update_fields=['status', 'error_message'])
        logger.error(
            "Upload failed for %s to %s: %s",
            date_from, date_to, log.error_message,
        )

    return log
