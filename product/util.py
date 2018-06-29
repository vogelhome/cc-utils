# Copyright (c) 2018 SAP SE or an SAP affiliate company. All rights reserved. This file is licensed under the Apache Software License, v. 2 except as noted otherwise in the LICENSE file
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from copy import deepcopy
import functools
import itertools
import yaml

from github.util import GitHubRepositoryHelper, github_api_ctor
from util import not_none
from .model import Product, COMPONENT_DESCRIPTOR_ASSET_NAME

class ComponentDescriptorResolver(object):
    def __init__(
        self,
        cfg_factory=None,
    ):
        self.cfg_factory=cfg_factory

    @functools.lru_cache()
    def _github_cfg_for_hostname(self, host_name):
        not_none(host_name)
        for github_cfg in self.cfg_factory._cfg_elements(cfg_type_name='github'):
            if github_cfg.matches_hostname(host_name=host_name):
                return github_cfg
        raise RuntimeError('no github_cfg for {h}'.format(host_name))

    @functools.lru_cache()
    def _github_api_for_hostname(self, host_name):
        not_none(host_name)
        # hard-code schema to https
        url = 'https://' + host_name
        ctor = github_api_ctor(github_url=url)
        return ctor()


    def _repository_helper(self, component_reference):
        gh_helper_ctor = functools.partial(
                GitHubRepositoryHelper,
                owner=component_reference.github_organisation(),
                name=component_reference.github_repo(),
        )

        if self.cfg_factory:
            return gh_helper_ctor(
                github_cfg=self._github_cfg_for_hostname(
                    host_name=component_reference.github_host(),
                )
            )
        else:
            return gh_helper_ctor(
                github_api=self._github_api_for_hostname(
                    host_name=component_reference.github_host(),
                )
            )

    def retrieve_raw_descriptor(self, component_reference, as_dict=False):
        repo_helper = self._repository_helper(component_reference)
        dependency_descriptor = repo_helper.retrieve_asset_contents(
                release_tag=component_reference.version(),
                asset_label=COMPONENT_DESCRIPTOR_ASSET_NAME,
            )
        if as_dict:
            return yaml.load(dependency_descriptor)
        else:
            return dependency_descriptor

    def retrieve_descriptor(self, component_reference):
        dependency_descriptor = self.retrieve_raw_descriptor(
            component_reference=component_reference,
            as_dict=True,
        )
        return Product.from_dict(dependency_descriptor)

    def resolve_component_references(
        self,
        product,
    ):
        def unresolved_references(component):
            component_references = component.dependencies().components()
            yield from filter(lambda cr: not product.component(cr), component_references)

        merged = Product.from_dict(raw_dict=deepcopy(dict(product.raw.items())))

        for component_reference in itertools.chain(*map(unresolved_references, product.components())):
            resolved_descriptor = self.retrieve_descriptor(component_reference)
            merged = merge_products(merged, resolved_descriptor)

        return merged



def merge_products(left_product, right_product):
    not_none(left_product)
    not_none(right_product)

    # start with a copy of left_product
    merged = Product.from_dict(raw_dict=deepcopy(dict(left_product.raw.items())))
    for component in right_product.components():
        existing_component = merged.component(component)
        if existing_component:
            # it is acceptable to add an existing component iff it is identical
            if existing_component.raw == component.raw:
                continue # skip
            else:
                raise ValueError(
                    'conflicting component definitions: {c1}, {c2}'.format(
                        c1=':'.join((existing_component.name(), existing_component.version())),
                        c2=':'.join((component.name(), component.version())),
                    )
                )
        merged.add_component(component)

    return merged



