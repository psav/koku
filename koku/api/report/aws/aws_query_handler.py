#
# Copyright 2018 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""AWS Query Handling for Reports."""
import copy

from django.db.models import (F,
                              Max,
                              Sum,
                              Window)
from django.db.models.functions import DenseRank
from tenant_schemas.utils import tenant_context

from api.report.queries import ReportQueryHandler

EXPORT_COLUMNS = ['cost_entry_id', 'cost_entry_bill_id',
                  'cost_entry_product_id', 'cost_entry_pricing_id',
                  'cost_entry_reservation_id', 'tags',
                  'invoice_id', 'line_item_type', 'usage_account_id',
                  'usage_start', 'usage_end', 'product_code',
                  'usage_type', 'operation', 'availability_zone',
                  'usage_amount', 'normalization_factor',
                  'normalized_usage_amount', 'currency_code',
                  'unblended_rate', 'unblended_cost', 'blended_rate',
                  'blended_cost', 'tax_type']


class AWSReportQueryHandler(ReportQueryHandler):
    """Handles report queries and responses for AWS."""

    default_ordering = {'total': 'desc'}
    group_by_options = ['service', 'account', 'region', 'avail_zone']

    def __init__(self, query_parameters, url_data,
                 tenant, **kwargs):
        """Establish AWS report query handler.

        Args:
            query_parameters    (Dict): parameters for query
            url_data        (String): URL string to provide order information
            tenant    (String): the tenant to use to access CUR data
            kwargs    (Dict): A dictionary for internal query alteration based on path
        """
        kwargs['provider'] = 'AWS'

        super().__init__(query_parameters, url_data,
                         tenant, self.default_ordering,
                         self.group_by_options, **kwargs)

    def _ranked_list(self, data_list):
        """Get list of ranked items less than top.

        Args:
            data_list (List(Dict)): List of ranked data points from the same bucket
        Returns:
            List(Dict): List of data points meeting the rank criteria
        """
        ranked_list = []
        others_list = []
        other = None
        other_sum = 0
        for data in data_list:
            if other is None:
                other = copy.deepcopy(data)
            rank = data.get('rank')
            if rank <= self._limit:
                del data['rank']
                ranked_list.append(data)
            else:
                others_list.append(data)
                total = data.get('total')
                if total:
                    other_sum += total

        if other is not None and others_list:
            other['total'] = other_sum
            del other['rank']
            group_by = self._get_group_by()
            for group in group_by:
                other[group] = 'Other'
            if 'account' in group_by:
                other['account_alias'] = 'Other'
            ranked_list.append(other)

        return ranked_list

    def _format_query_response(self):
        """Format the query response with data.

        Returns:
            (Dict): Dictionary response of query params, data, and total

        """
        output = copy.deepcopy(self.query_parameters)
        output['data'] = self.query_data
        output['total'] = self.query_sum

        if self._delta:
            output['delta'] = self.query_delta

        return output

    def execute_sum_query(self):
        """Execute query and return provided data when self.is_sum == True.

        Returns:
            (Dict): Dictionary response of query params, data, and total

        """
        query_sum = {'value': 0}
        data = []

        q_table = self._mapper._operation_map.get('tables').get('query')
        with tenant_context(self.tenant):
            query = q_table.objects.filter(self.query_filter)

            query_annotations = self._get_annotations()
            query_data = query.annotate(**query_annotations)

            query_group_by = ['date'] + self._get_group_by()

            query_order_by = ('-date', )
            if self.order_field != 'delta':
                query_order_by += (self.order,)

            aggregate_key = self._mapper._report_type_map.get('aggregate_key')
            query_data = query_data.values(*query_group_by)\
                .annotate(total=Sum(aggregate_key))\
                .annotate(units=Max(self._mapper.units_key))

            if 'account' in query_group_by:
                query_data = query_data.annotate(account_alias=F(self._mapper._operation_map.get('alias')))

            if self._mapper.count:
                # This is a sum because the summary table already
                # has already performed counts
                query_data = query_data.annotate(count=Sum(self._mapper.count))

            if self._limit:
                rank_order = getattr(F(self.order_field), self.order_direction)()
                dense_rank_by_total = Window(
                    expression=DenseRank(),
                    partition_by=F('date'),
                    order_by=rank_order
                )
                query_data = query_data.annotate(rank=dense_rank_by_total)
                query_order_by = query_order_by + ('rank',)

            if self.order_field != 'delta':
                query_data = query_data.order_by(*query_order_by)

            if query.exists():
                units_value = query.values(self._mapper.units_key)\
                                   .first().get(self._mapper.units_key)
                query_sum = self.calculate_total(units_value)

            if self._delta:
                query_data = self.add_deltas(query_data, query_sum)

            is_csv_output = self._accept_type and 'text/csv' in self._accept_type
            if is_csv_output:
                if self._limit:
                    data = self._ranked_list(list(query_data))
                else:
                    data = list(query_data)
            else:
                data = self._apply_group_by(list(query_data))
                data = self._transform_data(query_group_by, 0, data)

        self.query_sum = query_sum
        self.query_data = data
        return self._format_query_response()

    def execute_query(self):
        """Execute query and return provided data.

        Returns:
            (Dict): Dictionary response of query params, data, and total

        """
        if self.is_sum:
            return self.execute_sum_query()

        query_sum = {'value': 0}
        data = []

        q_table = self._mapper._operation_map.get('tables').get('query')
        with tenant_context(self.tenant):
            query = q_table.objects.filter(self.query_filter)

            query_annotations = self._get_annotations()
            query_data = query.annotate(**query_annotations)

            query_group_by = ['date'] + self._get_group_by()
            query_group_by_with_units = query_group_by + ['units']

            query_order_by = ('-date',)
            query_data = query_data.order_by(*query_order_by)
            values_out = query_group_by_with_units + EXPORT_COLUMNS
            data = list(query_data.values(*values_out))

            if query.exists():
                units_value = query.values(self._mapper.units_key)\
                                   .first().get(self._mapper.units_key)
                query_sum = self.calculate_total(units_value)

        self.query_sum = query_sum
        self.query_data = data
        return self._format_query_response()