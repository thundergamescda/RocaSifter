"""Fictional platform authority helpers, restricted to disposable namespaces."""
import re
import uuid
from hosted.owner_onboarding import OperatorAuthority


def operator_fixture(database):
    with database.transaction() as db:
        name=db.execute('SELECT current_schema() AS name').fetchone()['name']
        if not re.fullmatch('ss_test_[a-f0-9]{32}',name or ''):raise ValueError('Fictional platform authority requires a disposable test namespace.')
    return OperatorAuthority('fictional-platform-operator')


def issue_fixture(service,**changes):
    operator=operator_fixture(service.database);suffix=uuid.uuid4().hex
    data={'request_key':'fictional-issue-'+suffix,'email':'owner-'+suffix+'@example.invalid',
          'store_slug':'fictional-'+suffix,'store_name':'Fictional independent store',
          'reason':'Fictional platform operator approved a private test invitation','expires_seconds':86400}
    data.update(changes)
    return service.issue(operator,data),data


def accept_fixture(invitation,**changes):
    body={'invitation_token':invitation['invitation_token'],'email':invitation['email'],
          'password':'Fictional owner password only 2026!','request_key':'fictional-accept-'+uuid.uuid4().hex,
          'store_details_confirmed':True}
    body.update(changes);return body
