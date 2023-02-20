<%def name="include_pull_request_resource_type()">
<%
from concourse.client.model import ResourceType
%>
- name: ${ResourceType.PULL_REQUEST.value}
  type: docker-image
  source:
    repository: eu.gcr.io/gardener-project/cc/pr-resource
</%def>
