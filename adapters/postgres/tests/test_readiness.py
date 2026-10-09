from copy import deepcopy
import pytest
from prosaic_runtime.accounting import AccountingError
from prosaic_runtime_postgres import schema


def catalog():
    tables = [('prosaic_metering_intents', 'p', 'r'), ('prosaic_metering_observations', 'p', 'r')]
    columns = []
    for table, fields in [
        ('prosaic_metering_intents', [('namespace','text',True),('environment','text',True),
            ('provider_call_id','text',True),('payload','jsonb',True),('payload_hash','text',True),
            ('started_at','timestamp with time zone',True)]),
        ('prosaic_metering_observations', [('namespace','text',True),('environment','text',True),
            ('provider_call_id','text',True),('payload','jsonb',True),('payload_hash','text',True),
            ('assessment','jsonb',True),('amount','numeric(38,18)',False),('currency','text',False),
            ('measurement_state','text',True)])]:
        columns.extend((table,*field) for field in fields)
    constraints = [('prosaic_metering_intents','PRIMARY KEY (namespace, environment, provider_call_id)',True,'p'),
        ('prosaic_metering_observations','PRIMARY KEY (namespace, environment, provider_call_id)',True,'p'),
        ('prosaic_metering_observations','FOREIGN KEY (namespace, environment, provider_call_id) REFERENCES prosaic_metering_intents(namespace, environment, provider_call_id)',True,'f'),
        ('prosaic_metering_observations',"CHECK ((measurement_state = ANY (ARRAY['complete'::text, 'unresolved'::text, 'quarantined'::text, 'unsupported'::text])))",True,'c')]
    return tables, columns, constraints


def test_complete_contract_and_pg18_null_constraints_are_admitted():
    tables, columns, constraints = catalog()
    constraints.append(('prosaic_metering_intents','NOT NULL namespace',True,'n'))
    schema.validate_catalog(tables, columns, constraints)


@pytest.mark.parametrize('mutation', ['missing_column','wrong_type','nullable','unlogged','missing_key','unvalidated','wrong_state_check'])
def test_incompatible_contract_is_rejected_without_echoing_values(mutation):
    tables, columns, constraints = deepcopy(catalog())
    if mutation == 'missing_column': columns = [r for r in columns if r[1] != 'payload']
    elif mutation == 'wrong_type': columns[0] = ('prosaic_metering_intents','namespace','PRIVATE_SENTINEL',True)
    elif mutation == 'nullable': columns[0] = ('prosaic_metering_intents','namespace','text',False)
    elif mutation == 'unlogged': tables[0] = ('prosaic_metering_intents','u','r')
    elif mutation == 'missing_key': constraints.pop(0)
    elif mutation == 'unvalidated': constraints[0] = (*constraints[0][:2],False,'p')
    else: constraints[-1] = ('prosaic_metering_observations',"CHECK (measurement_state <> 'complete')",True,'c')
    with pytest.raises(AccountingError) as error:
        schema.validate_catalog(tables, columns, constraints)
    assert 'PRIVATE_SENTINEL' not in str(error.value)
