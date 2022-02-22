import dataclasses
import datetime
import logging
import os
import typing
import yaml

import dateutil.parser

import cfg_mgmt.model as cmm
import cfg_mgmt.metrics
import cfg_mgmt.reporting as cmr
import ci.util
import model


logger = logging.getLogger(__name__)


def generate_cfg_element_status_reports(cfg_dir: str) -> list[cmr.CfgElementStatusReport]:
    ci.util.existing_dir(cfg_dir)

    cfg_factory = model.ConfigFactory._from_cfg_dir(
        cfg_dir,
        disable_cfg_element_lookup=True,
    )

    cfg_metadata = cmm.cfg_metadata_from_cfg_dir(cfg_dir)

    policies = cfg_metadata.policies
    rules = cfg_metadata.rules
    statuses = cfg_metadata.statuses
    responsibles = cfg_metadata.responsibles

    return [
        determine_status(
            element=element,
            policies=policies,
            rules=rules,
            statuses=statuses,
            responsibles=responsibles,
            element_storage=cfg_dir,
        ) for element in iter_cfg_elements(cfg_factory=cfg_factory)
    ]


def iter_cfg_elements(
    cfg_factory: typing.Union[model.ConfigFactory, model.ConfigurationSet],
    cfg_target: typing.Optional[cmm.CfgTarget] = None,
):
    if isinstance(cfg_factory, model.ConfigurationSet):
        type_names = cfg_factory.cfg_factory._cfg_types().keys()
    else:
        type_names = cfg_factory._cfg_types().keys()

    for type_name in type_names:
        # workaround: cfg-sets may reference non-local cfg-elements
        # also, cfg-elements only contain references to other cfg-elements
        # -> policy-checks will only add limited value
        if type_name == 'cfg_set':
            continue
        for cfg_element in cfg_factory._cfg_elements(cfg_type_name=type_name):
            if cfg_target and not cfg_target.matches(cfg_element):
                continue
            yield cfg_element


def iter_cfg_queue_entries_to_be_deleted(
    cfg_metadata: cmm.CfgMetadata,
    cfg_target: typing.Optional[cmm.CfgTarget]=None,
) -> typing.Generator[cmm.CfgQueueEntry, None, None]:
    now = datetime.datetime.now()
    for cfg_queue_entry in cfg_metadata.queue:
        if cfg_target and not cfg_target == cfg_queue_entry.target:
            continue

        if not cfg_queue_entry.to_be_deleted(now):
            continue

        yield cfg_queue_entry


def iter_cfg_elements_requiring_rotation(
    cfg_elements: typing.Iterable[model.NamedModelElement],
    cfg_metadata: cmm.CfgMetadata,
    cfg_target: typing.Optional[cmm.CfgTarget]=None,
    element_filter: typing.Callable[[model.NamedModelElement], bool]=None,
    rotation_method: cmm.RotationMethod=None,
) -> typing.Generator[model.NamedModelElement, None, None]:
    for cfg_element in cfg_elements:
        if cfg_target and not cfg_target.matches(element=cfg_element):
            continue

        if element_filter and not element_filter(cfg_element):
            continue

        status = determine_status(
            element=cfg_element,
            policies=cfg_metadata.policies,
            rules=cfg_metadata.rules,
            responsibles=cfg_metadata.responsibles,
            statuses=cfg_metadata.statuses,
        )

        # hardcode rule: ignore elements w/o rule and policy
        if not status.policy or not status.rule:
            continue

        # hardcode: ignore all policies we cannot handle (currently, only MAX_AGE)
        if not status.policy.type is cmm.PolicyType.MAX_AGE:
            continue

        if rotation_method and status.policy.rotation_method is not rotation_method:
            continue

        # if there is no status, assume rotation be required
        if not status.status:
            yield cfg_element
            continue

        last_update = dateutil.parser.isoparse(status.status.credential_update_timestamp)
        if status.policy.check(last_update=last_update):
            continue
        else:
            yield cfg_element


def determine_status(
    element: model.NamedModelElement,
    policies: list[cmm.CfgPolicy],
    rules: list[cmm.CfgRule],
    responsibles: list[cmm.CfgResponsibleMapping],
    statuses: list[cmm.CfgStatus],
    element_storage: str=None,
) -> cmr.CfgElementStatusReport:
    for rule in rules:
        if rule.matches(element=element):
            break
    else:
        rule = None # no rule was configured

    rule: typing.Optional[cmm.CfgRule]

    if rule:
        for policy in policies:
            if policy.name == rule.policy:
                break
        else:
            rule = None # inconsistent cfg: rule with specified name does not exist

    for responsible in responsibles:
        if responsible.matches(element=element):
            break
    else:
        responsible = None

    for status in statuses:
        if status.matches(element):
            break
    else:
        status = None

    return cmr.CfgElementStatusReport(
        element_storage=element_storage,
        element_type=element._type_name,
        element_name=element._name,
        policy=policy,
        rule=rule,
        status=status,
        responsible=responsible,
    )


def create_config_queue_entry(
    queue_entry_config_element: model.NamedModelElement,
    queue_entry_data: dict,
) -> cmm.CfgQueueEntry:
    return cmm.CfgQueueEntry(
        target=cmm.CfgTarget(
            name=queue_entry_config_element.name(),
            type=queue_entry_config_element._type_name,
        ),
        deleteAfter=(datetime.datetime.today() + datetime.timedelta(days=7)).date().isoformat(),
        secretId=queue_entry_data,
    )


def update_config_status(
    cfg_status_file_path: str,
    config_element: model.NamedModelElement,
    config_statuses: typing.Iterable[cmm.CfgStatus],
):
    for cfg_status in config_statuses:
        if cfg_status.matches(
            element=config_element,
        ):
            break
    else:
        # does not exist
        cfg_status = cmm.CfgStatus(
            target=cmm.CfgTarget(
                type=config_element._type_name,
                name=config_element.name(),
            ),
            credential_update_timestamp=datetime.date.today().isoformat(),
        )
        config_statuses.append(cfg_status)
    cfg_status.credential_update_timestamp = datetime.date.today().isoformat()

    with open(cfg_status_file_path, 'w') as f:
        yaml.dump(
            {
                'config_status': [
                    dataclasses.asdict(cfg_status)
                    for cfg_status in config_statuses
                ]
            },
            f,
        )


def write_config_queue(
    cfg_dir: str,
    cfg_metadata: cmm.CfgMetadata,
    queue_file_name: str=cmm.cfg_queue_fname,
):
    with open(os.path.join(cfg_dir, queue_file_name), 'w') as queue_file:
        yaml.dump(
            {
                'rotation_queue': [
                    dataclasses.asdict(cfg_queue_entry)
                    for cfg_queue_entry in cfg_metadata.queue
                ],
            },
            queue_file,
            Dumper=ci.util.MultilineYamlDumper,
        )


def cfg_report_summaries_to_es(
    es_client,
    cfg_report_summary_gen: typing.Generator[cmm.CfgReportingSummary, None, None],
):
    for cfg_report_summary in cfg_report_summary_gen:
        cc_cfg_compliance_status = cfg_mgmt.metrics.CcCfgComplianceStatus.create(
            url=cfg_report_summary.url,
            compliant_count=cfg_report_summary.compliantElementsCount,
            non_compliant_count=cfg_report_summary.noncompliantElementsCount,
        )

        cfg_mgmt.metrics.metric_to_es(
            es_client=es_client,
            metric=cc_cfg_compliance_status,
        )


def cfg_element_statuses_to_es(
    es_client,
    cfg_element_statuses: typing.Iterable[cmr.CfgElementStatusReport],
):
    for cfg_element_status in cfg_element_statuses:
        # HACK
        # We only use create_report to determine is_compliant flag.
        # Therefore the amount of compliant elements is considered.
        # As we only pass one cfg_element_status to create_report,
        # the amount of compliant elements is either 0 or 1
        report = next(cmr.create_report(
            cfg_element_statuses=[cfg_element_status],
            print_report=False,
        ))

        cc_cfg_compliance_responsible = cfg_mgmt.metrics.CcCfgComplianceResponsible.create(
            element_name=cfg_element_status.element_name,
            element_type=cfg_element_status.element_type,
            element_storage=cfg_element_status.element_storage,
            is_compliant=bool(report.compliantElementsCount),
            responsible=cfg_element_status.responsible,
            rotation_method=cfg_element_status.policy.rotation_method,
        )

        cfg_mgmt.metrics.metric_to_es(
            es_client=es_client,
            metric=cc_cfg_compliance_responsible,
        )


def local_cfg_type_sources(
    cfg_element: model.NamedModelElement,
    cfg_factory: typing.Union[model.ConfigFactory, model.ConfigurationSet],
) -> typing.Iterable[str]:
    cfg_type = cfg_factory._cfg_type(cfg_element._type_name)
    return {
        src.file for src in cfg_type.sources() if isinstance(src, model.LocalFileCfgSrc)
    }
