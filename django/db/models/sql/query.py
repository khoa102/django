"""
Create SQL statements for QuerySets.

The code in here encapsulates all of the SQL construction so that QuerySets
themselves do not have to (and could be backed by things other than SQL
databases). The abstraction barrier only works one way: this module has to know
all about the internals of models in order to get the information it needs.
"""

import copy

from django.utils.datastructures import SortedDict
from django.utils.encoding import force_text
from django.utils.tree import Node
from django.utils import six
from django.db import connections, DEFAULT_DB_ALIAS
from django.db.models import signals
from django.db.models.constants import LOOKUP_SEP
from django.db.models.expressions import ExpressionNode
from django.db.models.fields import FieldDoesNotExist
from django.db.models.sql import aggregates as base_aggregates_module
from django.db.models.sql.constants import (QUERY_TERMS, ORDER_DIR, SINGLE,
        ORDER_PATTERN, REUSE_ALL, JoinInfo, SelectInfo)
from django.db.models.sql.datastructures import EmptyResultSet, Empty, MultiJoin
from django.db.models.sql.expressions import SQLEvaluator
from django.db.models.sql.where import (WhereNode, Constraint, EverythingNode,
    ExtraWhere, AND, OR)
from django.core.exceptions import FieldError

__all__ = ['Query', 'RawQuery']


class RawQuery(object):
    """
    A single raw SQL query
    """

    def __init__(self, sql, using, params=None):
        self.params = params or ()
        self.sql = sql
        self.using = using
        self.cursor = None

        # Mirror some properties of a normal query so that
        # the compiler can be used to process results.
        self.low_mark, self.high_mark = 0, None  # Used for offset/limit
        self.extra_select = {}
        self.aggregate_select = {}

    def clone(self, using):
        return RawQuery(self.sql, using, params=self.params)

    def convert_values(self, value, field, connection):
        """Convert the database-returned value into a type that is consistent
        across database backends.

        By default, this defers to the underlying backend operations, but
        it can be overridden by Query classes for specific backends.
        """
        return connection.ops.convert_values(value, field)

    def get_columns(self):
        if self.cursor is None:
            self._execute_query()
        converter = connections[self.using].introspection.table_name_converter
        return [converter(column_meta[0])
                for column_meta in self.cursor.description]

    def __iter__(self):
        # Always execute a new query for a new iterator.
        # This could be optimized with a cache at the expense of RAM.
        self._execute_query()
        if not connections[self.using].features.can_use_chunked_reads:
            # If the database can't use chunked reads we need to make sure we
            # evaluate the entire query up front.
            result = list(self.cursor)
        else:
            result = self.cursor
        return iter(result)

    def __repr__(self):
        return "<RawQuery: %r>" % (self.sql % tuple(self.params))

    def _execute_query(self):
        self.cursor = connections[self.using].cursor()
        self.cursor.execute(self.sql, self.params)


class Query(object):
    """
    A single SQL query.
    """
    # SQL join types. These are part of the class because their string forms
    # vary from database to database and can be customised by a subclass.
    INNER = 'INNER JOIN'
    LOUTER = 'LEFT OUTER JOIN'

    alias_prefix = 'T'
    query_terms = QUERY_TERMS
    aggregates_module = base_aggregates_module

    compiler = 'SQLCompiler'

    def __init__(self, model, where=WhereNode):
        self.model = model
        self.alias_refcount = SortedDict()
        # alias_map is the most important data structure regarding joins.
        # It's used for recording which joins exist in the query and what
        # type they are. The key is the alias of the joined table (possibly
        # the table name) and the value is JoinInfo from constants.py.
        self.alias_map = {}
        self.table_map = {}     # Maps table names to list of aliases.
        self.join_map = {}
        self.default_cols = True
        self.default_ordering = True
        self.standard_ordering = True
        self.ordering_aliases = []
        self.used_aliases = set()
        self.filter_is_sticky = False
        self.included_inherited_models = {}

        # SQL-related attributes  
        # Select and related select clauses as SelectInfo instances.
        # The select is used for cases where we want to set up the select
        # clause to contain other than default fields (values(), annotate(),
        # subqueries...)
        self.select = []
        # The related_select_cols is used for columns needed for
        # select_related - this is populated in compile stage.
        self.related_select_cols = []
        self.tables = []    # Aliases in the order they are created.
        self.where = where()
        self.where_class = where
        self.group_by = None
        self.having = where()
        self.order_by = []
        self.low_mark, self.high_mark = 0, None  # Used for offset/limit
        self.distinct = False
        self.distinct_fields = []
        self.select_for_update = False
        self.select_for_update_nowait = False
        self.select_related = False

        # SQL aggregate-related attributes
        self.aggregates = SortedDict() # Maps alias -> SQL aggregate function
        self.aggregate_select_mask = None
        self._aggregate_select_cache = None

        # Arbitrary maximum limit for select_related. Prevents infinite
        # recursion. Can be changed by the depth parameter to select_related().
        self.max_depth = 5

        # These are for extensions. The contents are more or less appended
        # verbatim to the appropriate clause.
        self.extra = SortedDict()  # Maps col_alias -> (col_sql, params).
        self.extra_select_mask = None
        self._extra_select_cache = None

        self.extra_tables = ()
        self.extra_order_by = ()

        # A tuple that is a set of model field names and either True, if these
        # are the fields to defer, or False if these are the only fields to
        # load.
        self.deferred_loading = (set(), True)

    def __str__(self):
        """
        Returns the query as a string of SQL with the parameter values
        substituted in (use sql_with_params() to see the unsubstituted string).

        Parameter values won't necessarily be quoted correctly, since that is
        done by the database interface at execution time.
        """
        sql, params = self.sql_with_params()
        return sql % params

    def sql_with_params(self):
        """
        Returns the query as an SQL string and the parameters that will be
        subsituted into the query.
        """
        return self.get_compiler(DEFAULT_DB_ALIAS).as_sql()

    def __deepcopy__(self, memo):
        result = self.clone(memo=memo)
        memo[id(self)] = result
        return result

    def __getstate__(self):
        """
        Pickling support.
        """
        obj_dict = self.__dict__.copy()
        obj_dict['related_select_cols'] = []

        # Fields can't be pickled, so if a field list has been
        # specified, we pickle the list of field names instead.
        # None is also a possible value; that can pass as-is
        obj_dict['select'] = [
            (s.col, s.field is not None and s.field.name or None)
            for s in obj_dict['select']
        ]
        return obj_dict

    def __setstate__(self, obj_dict):
        """
        Unpickling support.
        """
        # Rebuild list of field instances
        opts = obj_dict['model']._meta
        obj_dict['select'] = [
            SelectInfo(tpl[0], tpl[1] is not None and opts.get_field(tpl[1]) or None)
            for tpl in obj_dict['select']
        ]

        self.__dict__.update(obj_dict)

    def prepare(self):
        return self

    def get_compiler(self, using=None, connection=None):
        if using is None and connection is None:
            raise ValueError("Need either using or connection")
        if using:
            connection = connections[using]

        # Check that the compiler will be able to execute the query
        for alias, aggregate in self.aggregate_select.items():
            connection.ops.check_aggregate_support(aggregate)

        return connection.ops.compiler(self.compiler)(self, connection, using)

    def get_meta(self):
        """
        Returns the Options instance (the model._meta) from which to start
        processing. Normally, this is self.model._meta, but it can be changed
        by subclasses.
        """
        return self.model._meta

    def clone(self, klass=None, memo=None, **kwargs):
        """
        Creates a copy of the current instance. The 'kwargs' parameter can be
        used by clients to update attributes after copying has taken place.
        """
        obj = Empty()
        obj.__class__ = klass or self.__class__
        obj.model = self.model
        obj.alias_refcount = self.alias_refcount.copy()
        obj.alias_map = self.alias_map.copy()
        obj.table_map = self.table_map.copy()
        obj.join_map = self.join_map.copy()
        obj.default_cols = self.default_cols
        obj.default_ordering = self.default_ordering
        obj.standard_ordering = self.standard_ordering
        obj.included_inherited_models = self.included_inherited_models.copy()
        obj.ordering_aliases = []
        obj.select = self.select[:]
        obj.related_select_cols = []
        obj.tables = self.tables[:]
        obj.where = copy.deepcopy(self.where, memo=memo)
        obj.where_class = self.where_class
        if self.group_by is None:
            obj.group_by = None
        else:
            obj.group_by = self.group_by[:]
        obj.having = copy.deepcopy(self.having, memo=memo)
        obj.order_by = self.order_by[:]
        obj.low_mark, obj.high_mark = self.low_mark, self.high_mark
        obj.distinct = self.distinct
        obj.distinct_fields = self.distinct_fields[:]
        obj.select_for_update = self.select_for_update
        obj.select_for_update_nowait = self.select_for_update_nowait
        obj.select_related = self.select_related
        obj.aggregates = copy.deepcopy(self.aggregates, memo=memo)
        if self.aggregate_select_mask is None:
            obj.aggregate_select_mask = None
        else:
            obj.aggregate_select_mask = self.aggregate_select_mask.copy()
        # _aggregate_select_cache cannot be copied, as doing so breaks the
        # (necessary) state in which both aggregates and
        # _aggregate_select_cache point to the same underlying objects.
        # It will get re-populated in the cloned queryset the next time it's
        # used.
        obj._aggregate_select_cache = None
        obj.max_depth = self.max_depth
        obj.extra = self.extra.copy()
        if self.extra_select_mask is None:
            obj.extra_select_mask = None
        else:
            obj.extra_select_mask = self.extra_select_mask.copy()
        if self._extra_select_cache is None:
            obj._extra_select_cache = None
        else:
            obj._extra_select_cache = self._extra_select_cache.copy()
        obj.extra_tables = self.extra_tables
        obj.extra_order_by = self.extra_order_by
        obj.deferred_loading = copy.deepcopy(self.deferred_loading, memo=memo)
        if self.filter_is_sticky and self.used_aliases:
            obj.used_aliases = self.used_aliases.copy()
        else:
            obj.used_aliases = set()
        obj.filter_is_sticky = False

        obj.__dict__.update(kwargs)
        if hasattr(obj, '_setup_query'):
            obj._setup_query()
        return obj

    def convert_values(self, value, field, connection):
        """Convert the database-returned value into a type that is consistent
        across database backends.

        By default, this defers to the underlying backend operations, but
        it can be overridden by Query classes for specific backends.
        """
        return connection.ops.convert_values(value, field)

    def resolve_aggregate(self, value, aggregate, connection):
        """Resolve the value of aggregates returned by the database to
        consistent (and reasonable) types.

        This is required because of the predisposition of certain backends
        to return Decimal and long types when they are not needed.
        """
        if value is None:
            if aggregate.is_ordinal:
                return 0
            # Return None as-is
            return value
        elif aggregate.is_ordinal:
            # Any ordinal aggregate (e.g., count) returns an int
            return int(value)
        elif aggregate.is_computed:
            # Any computed aggregate (e.g., avg) returns a float
            return float(value)
        else:
            # Return value depends on the type of the field being processed.
            return self.convert_values(value, aggregate.field, connection)

    def get_aggregation(self, using):
        """
        Returns the dictionary with the values of the existing aggregations.
        """
        if not self.aggregate_select:
            return {}

        # If there is a group by clause, aggregating does not add useful
        # information but retrieves only the first row. Aggregate
        # over the subquery instead.
        if self.group_by is not None:
            from django.db.models.sql.subqueries import AggregateQuery
            query = AggregateQuery(self.model)

            obj = self.clone()

            # Remove any aggregates marked for reduction from the subquery
            # and move them to the outer AggregateQuery.
            for alias, aggregate in self.aggregate_select.items():
                if aggregate.is_summary:
                    query.aggregate_select[alias] = aggregate
                    del obj.aggregate_select[alias]

            try:
                query.add_subquery(obj, using)
            except EmptyResultSet:
                return dict(
                    (alias, None)
                    for alias in query.aggregate_select
                )
        else:
            query = self
            self.select = []
            self.default_cols = False
            self.extra = {}
            self.remove_inherited_models()

        query.clear_ordering(True)
        query.clear_limits()
        query.select_for_update = False
        query.select_related = False
        query.related_select_cols = []

        result = query.get_compiler(using).execute_sql(SINGLE)
        if result is None:
            result = [None for q in query.aggregate_select.items()]

        return dict([
            (alias, self.resolve_aggregate(val, aggregate, connection=connections[using]))
            for (alias, aggregate), val
            in zip(query.aggregate_select.items(), result)
        ])

    def get_count(self, using):
        """
        Performs a COUNT() query using the current filter constraints.
        """
        obj = self.clone()
        if len(self.select) > 1 or self.aggregate_select or (self.distinct and self.distinct_fields):
            # If a select clause exists, then the query has already started to
            # specify the columns that are to be returned.
            # In this case, we need to use a subquery to evaluate the count.
            from django.db.models.sql.subqueries import AggregateQuery
            subquery = obj
            subquery.clear_ordering(True)
            subquery.clear_limits()

            obj = AggregateQuery(obj.model)
            try:
                obj.add_subquery(subquery, using=using)
            except EmptyResultSet:
                # add_subquery evaluates the query, if it's an EmptyResultSet
                # then there are can be no results, and therefore there the
                # count is obviously 0
                return 0

        obj.add_count_column()
        number = obj.get_aggregation(using=using)[None]

        # Apply offset and limit constraints manually, since using LIMIT/OFFSET
        # in SQL (in variants that provide them) doesn't change the COUNT
        # output.
        number = max(0, number - self.low_mark)
        if self.high_mark is not None:
            number = min(number, self.high_mark - self.low_mark)

        return number

    def has_results(self, using):
        q = self.clone()
        q.clear_select_clause()
        q.add_extra({'a': 1}, None, None, None, None, None)
        q.set_extra_mask(['a'])
        q.clear_ordering(True)
        q.set_limits(high=1)
        compiler = q.get_compiler(using=using)
        return bool(compiler.execute_sql(SINGLE))

    def combine(self, rhs, connector):
        """
        Merge the 'rhs' query into the current one (with any 'rhs' effects
        being applied *after* (that is, "to the right of") anything in the
        current query. 'rhs' is not modified during a call to this function.

        The 'connector' parameter describes how to connect filters from the
        'rhs' query.
        """
        assert self.model == rhs.model, \
                "Cannot combine queries on two different base models."
        assert self.can_filter(), \
                "Cannot combine queries once a slice has been taken."
        assert self.distinct == rhs.distinct, \
            "Cannot combine a unique query with a non-unique query."
        assert self.distinct_fields == rhs.distinct_fields, \
            "Cannot combine queries with different distinct fields."

        self.remove_inherited_models()
        # Work out how to relabel the rhs aliases, if necessary.
        change_map = {}
        conjunction = (connector == AND)

        # Determine which existing joins can be reused. When combining the
        # query with AND we must recreate all joins for m2m filters. When
        # combining with OR we can reuse joins. The reason is that in AND
        # case a single row can't fulfill a condition like:
        #     revrel__col=1 & revrel__col=2
        # But, there might be two different related rows matching this
        # condition. In OR case a single True is enough, so single row is
        # enough, too.
        #
        # Note that we will be creating duplicate joins for non-m2m joins in
        # the AND case. The results will be correct but this creates too many
        # joins. This is something that could be fixed later on.
        reuse = set() if conjunction else set(self.tables)
        # Base table must be present in the query - this is the same
        # table on both sides.
        self.get_initial_alias()
        # Now, add the joins from rhs query into the new query (skipping base
        # table).
        for alias in rhs.tables[1:]:
            if not rhs.alias_refcount[alias]:
                continue
            table, _, join_type, lhs, lhs_col, col, nullable = rhs.alias_map[alias]
            promote = (join_type == self.LOUTER)
            # If the left side of the join was already relabeled, use the
            # updated alias.
            lhs = change_map.get(lhs, lhs)
            new_alias = self.join(
                (lhs, table, lhs_col, col), reuse=reuse, promote=promote,
                outer_if_first=not conjunction, nullable=nullable)
            # We can't reuse the same join again in the query. If we have two
            # distinct joins for the same connection in rhs query, then the
            # combined query must have two joins, too.
            reuse.discard(new_alias)
            change_map[alias] = new_alias

        # So that we don't exclude valid results in an "or" query combination,
        # all joins exclusive to either the lhs or the rhs must be converted
        # to an outer join.
        if not conjunction:
            l_tables = set(self.tables)
            r_tables = set(rhs.tables)
            # Update r_tables aliases.
            for alias in change_map:
                if alias in r_tables:
                    # r_tables may contain entries that have a refcount of 0
                    # if the query has references to a table that can be
                    # trimmed because only the foreign key is used.
                    # We only need to fix the aliases for the tables that
                    # actually have aliases.
                    if rhs.alias_refcount[alias]:
                        r_tables.remove(alias)
                        r_tables.add(change_map[alias])
            # Find aliases that are exclusive to rhs or lhs.
            # These are promoted to outer joins.
            outer_tables = (l_tables | r_tables) - (l_tables & r_tables)
            for alias in outer_tables:
                # Again, some of the tables won't have aliases due to
                # the trimming of unnecessary tables.
                if self.alias_refcount.get(alias) or rhs.alias_refcount.get(alias):
                    self.promote_joins([alias], True)

        # Now relabel a copy of the rhs where-clause and add it to the current
        # one.
        if rhs.where:
            w = copy.deepcopy(rhs.where)
            w.relabel_aliases(change_map)
            if not self.where:
                # Since 'self' matches everything, add an explicit "include
                # everything" where-constraint so that connections between the
                # where clauses won't exclude valid results.
                self.where.add(EverythingNode(), AND)
        elif self.where:
            # rhs has an empty where clause.
            w = self.where_class()
            w.add(EverythingNode(), AND)
        else:
            w = self.where_class()
        self.where.add(w, connector)

        # Selection columns and extra extensions are those provided by 'rhs'.
        self.select = []
        for col, field in rhs.select:
            if isinstance(col, (list, tuple)):
                new_col = change_map.get(col[0], col[0]), col[1]
                self.select.append(SelectInfo(new_col, field))
            else:
                item = copy.deepcopy(col)
                item.relabel_aliases(change_map)
                self.select.append(SelectInfo(item, field))

        if connector == OR:
            # It would be nice to be able to handle this, but the queries don't
            # really make sense (or return consistent value sets). Not worth
            # the extra complexity when you can write a real query instead.
            if self.extra and rhs.extra:
                raise ValueError("When merging querysets using 'or', you "
                        "cannot have extra(select=...) on both sides.")
        self.extra.update(rhs.extra)
        extra_select_mask = set()
        if self.extra_select_mask is not None:
            extra_select_mask.update(self.extra_select_mask)
        if rhs.extra_select_mask is not None:
            extra_select_mask.update(rhs.extra_select_mask)
        if extra_select_mask:
            self.set_extra_mask(extra_select_mask)
        self.extra_tables += rhs.extra_tables

        # Ordering uses the 'rhs' ordering, unless it has none, in which case
        # the current ordering is used.
        self.order_by = rhs.order_by and rhs.order_by[:] or self.order_by
        self.extra_order_by = rhs.extra_order_by or self.extra_order_by

    def deferred_to_data(self, target, callback):
        """
        Converts the self.deferred_loading data structure to an alternate data
        structure, describing the field that *will* be loaded. This is used to
        compute the columns to select from the database and also by the
        QuerySet class to work out which fields are being initialised on each
        model. Models that have all their fields included aren't mentioned in
        the result, only those that have field restrictions in place.

        The "target" parameter is the instance that is populated (in place).
        The "callback" is a function that is called whenever a (model, field)
        pair need to be added to "target". It accepts three parameters:
        "target", and the model and list of fields being added for that model.
        """
        field_names, defer = self.deferred_loading
        if not field_names:
            return
        orig_opts = self.model._meta
        seen = {}
        must_include = {orig_opts.concrete_model: set([orig_opts.pk])}
        for field_name in field_names:
            parts = field_name.split(LOOKUP_SEP)
            cur_model = self.model
            opts = orig_opts
            for name in parts[:-1]:
                old_model = cur_model
                source = opts.get_field_by_name(name)[0]
                cur_model = source.rel.to
                opts = cur_model._meta
                # Even if we're "just passing through" this model, we must add
                # both the current model's pk and the related reference field
                # to the things we select.
                must_include[old_model].add(source)
                add_to_dict(must_include, cur_model, opts.pk)
            field, model, _, _ = opts.get_field_by_name(parts[-1])
            if model is None:
                model = cur_model
            add_to_dict(seen, model, field)

        if defer:
            # We need to load all fields for each model, except those that
            # appear in "seen" (for all models that appear in "seen"). The only
            # slight complexity here is handling fields that exist on parent
            # models.
            workset = {}
            for model, values in six.iteritems(seen):
                for field, m in model._meta.get_fields_with_model():
                    if field in values:
                        continue
                    add_to_dict(workset, m or model, field)
            for model, values in six.iteritems(must_include):
                # If we haven't included a model in workset, we don't add the
                # corresponding must_include fields for that model, since an
                # empty set means "include all fields". That's why there's no
                # "else" branch here.
                if model in workset:
                    workset[model].update(values)
            for model, values in six.iteritems(workset):
                callback(target, model, values)
        else:
            for model, values in six.iteritems(must_include):
                if model in seen:
                    seen[model].update(values)
                else:
                    # As we've passed through this model, but not explicitly
                    # included any fields, we have to make sure it's mentioned
                    # so that only the "must include" fields are pulled in.
                    seen[model] = values
            # Now ensure that every model in the inheritance chain is mentioned
            # in the parent list. Again, it must be mentioned to ensure that
            # only "must include" fields are pulled in.
            for model in orig_opts.get_parent_list():
                if model not in seen:
                    seen[model] = set()
            for model, values in six.iteritems(seen):
                callback(target, model, values)


    def deferred_to_columns_cb(self, target, model, fields):
        """
        Callback used by deferred_to_columns(). The "target" parameter should
        be a set instance.
        """
        table = model._meta.db_table
        if table not in target:
            target[table] = set()
        for field in fields:
            target[table].update(field.columns)


    def table_alias(self, table_name, create=False):
        """
        Returns a table alias for the given table_name and whether this is a
        new alias or not.

        If 'create' is true, a new alias is always created. Otherwise, the
        most recently created alias for the table (if one exists) is reused.
        """
        current = self.table_map.get(table_name)
        if not create and current:
            alias = current[0]
            self.alias_refcount[alias] += 1
            return alias, False

        # Create a new alias for this table.
        if current:
            alias = '%s%d' % (self.alias_prefix, len(self.alias_map) + 1)
            current.append(alias)
        else:
            # The first occurence of a table uses the table name directly.
            alias = table_name
            self.table_map[alias] = [alias]
        self.alias_refcount[alias] = 1
        self.tables.append(alias)
        return alias, True

    def ref_alias(self, alias):
        """ Increases the reference count for this alias. """
        self.alias_refcount[alias] += 1

    def unref_alias(self, alias, amount=1):
        """ Decreases the reference count for this alias. """
        self.alias_refcount[alias] -= amount

    def promote_joins(self, aliases, unconditional=False):
        """
        Promotes recursively the join type of given aliases and its children to
        an outer join. If 'unconditional' is False, the join is only promoted if
        it is nullable or the parent join is an outer join.

        Note about join promotion: When promoting any alias, we make sure all
        joins which start from that alias are promoted, too. When adding a join
        in join(), we make sure any join added to already existing LOUTER join
        is generated as LOUTER. This ensures we don't ever have broken join
        chains which contain first a LOUTER join, then an INNER JOIN, that is
        this kind of join should never be generated: a LOUTER b INNER c. The
        reason for avoiding this type of join chain is that the INNER after
        the LOUTER will effectively remove any effect the LOUTER had.
        """
        aliases = list(aliases)
        while aliases:
            alias = aliases.pop(0)
            if self.alias_map[alias].rhs_join_col is None:
                # This is the base table (first FROM entry) - this table
                # isn't really joined at all in the query, so we should not
                # alter its join type.
                continue
            parent_alias = self.alias_map[alias].lhs_alias
            parent_louter = (parent_alias
                and self.alias_map[parent_alias].join_type == self.LOUTER)
            already_louter = self.alias_map[alias].join_type == self.LOUTER
            if ((unconditional or self.alias_map[alias].nullable
                 or parent_louter) and not already_louter):
                data = self.alias_map[alias]._replace(join_type=self.LOUTER)
                self.alias_map[alias] = data
                # Join type of 'alias' changed, so re-examine all aliases that
                # refer to this one.
                aliases.extend(
                    join for join in self.alias_map.keys()
                    if (self.alias_map[join].lhs_alias == alias
                        and join not in aliases))

    def reset_refcounts(self, to_counts):
        """
        This method will reset reference counts for aliases so that they match
        the value passed in :param to_counts:.
        """
        for alias, cur_refcount in self.alias_refcount.copy().items():
            unref_amount = cur_refcount - to_counts.get(alias, 0)
            self.unref_alias(alias, unref_amount)

    def promote_unused_aliases(self, initial_refcounts, used_aliases):
        """
        Given a "before" copy of the alias_refcounts dictionary (as
        'initial_refcounts') and a collection of aliases that may have been
        changed or created, works out which aliases have been created since
        then and which ones haven't been used and promotes all of those
        aliases, plus any children of theirs in the alias tree, to outer joins.
        """
        for alias in self.tables:
            if alias in used_aliases and (alias not in initial_refcounts or
                    self.alias_refcount[alias] == initial_refcounts[alias]):
                self.promote_joins([alias])

    def change_aliases(self, change_map):
        """
        Changes the aliases in change_map (which maps old-alias -> new-alias),
        relabelling any references to them in select columns and the where
        clause.
        """
        assert set(change_map.keys()).intersection(set(change_map.values())) == set()

        def relabel_column(col):
            if isinstance(col, (list, tuple)):
                old_alias = col[0]
                return (change_map.get(old_alias, old_alias), col[1])
            else:
                col.relabel_aliases(change_map)
                return col
        # 1. Update references in "select" (normal columns plus aliases),
        # "group by", "where" and "having".
        self.where.relabel_aliases(change_map)
        self.having.relabel_aliases(change_map)
        if self.group_by:
            self.group_by = [relabel_column(col) for col in self.group_by]
        self.select = [SelectInfo(relabel_column(s.col), s.field)
                       for s in self.select]
        self.aggregates = SortedDict(
            (key, relabel_column(col)) for key, col in self.aggregates.items())

        # 2. Rename the alias in the internal table/alias datastructures.
        for ident, aliases in self.join_map.items():
            del self.join_map[ident]
            aliases = tuple([change_map.get(a, a) for a in aliases])
            ident = (change_map.get(ident[0], ident[0]),) + ident[1:]
            self.join_map[ident] = aliases
        for old_alias, new_alias in six.iteritems(change_map):
            alias_data = self.alias_map[old_alias]
            alias_data = alias_data._replace(rhs_alias=new_alias)
            self.alias_refcount[new_alias] = self.alias_refcount[old_alias]
            del self.alias_refcount[old_alias]
            self.alias_map[new_alias] = alias_data
            del self.alias_map[old_alias]

            table_aliases = self.table_map[alias_data.table_name]
            for pos, alias in enumerate(table_aliases):
                if alias == old_alias:
                    table_aliases[pos] = new_alias
                    break
            for pos, alias in enumerate(self.tables):
                if alias == old_alias:
                    self.tables[pos] = new_alias
                    break
        for key, alias in self.included_inherited_models.items():
            if alias in change_map:
                self.included_inherited_models[key] = change_map[alias]

        # 3. Update any joins that refer to the old alias.
        for alias, data in six.iteritems(self.alias_map):
            lhs = data.lhs_alias
            if lhs in change_map:
                data = data._replace(lhs_alias=change_map[lhs])
                self.alias_map[alias] = data

    def bump_prefix(self, exceptions=()):
        """
        Changes the alias prefix to the next letter in the alphabet and
        relabels all the aliases. Even tables that previously had no alias will
        get an alias after this call (it's mostly used for nested queries and
        the outer query will already be using the non-aliased table name).

        Subclasses who create their own prefix should override this method to
        produce a similar result (a new prefix and relabelled aliases).

        The 'exceptions' parameter is a container that holds alias names which
        should not be changed.
        """
        current = ord(self.alias_prefix)
        assert current < ord('Z')
        prefix = chr(current + 1)
        self.alias_prefix = prefix
        change_map = SortedDict()
        for pos, alias in enumerate(self.tables):
            if alias in exceptions:
                continue
            new_alias = '%s%d' % (prefix, pos)
            change_map[alias] = new_alias
            self.tables[pos] = new_alias
        self.change_aliases(change_map)

    def get_initial_alias(self):
        """
        Returns the first alias for this query, after increasing its reference
        count.
        """
        if self.tables:
            alias = self.tables[0]
            self.ref_alias(alias)
        else:
            alias = self.join((None, self.model._meta.db_table, None, None))
        return alias

    def count_active_tables(self):
        """
        Returns the number of tables in this query with a non-zero reference
        count. Note that after execution, the reference counts are zeroed, so
        tables added in compiler will not be seen by this method.
        """
        return len([1 for count in six.itervalues(self.alias_refcount) if count])

    def join(self, connection, reuse=REUSE_ALL, promote=False,
             outer_if_first=False, nullable=False):
        """
        Returns an alias for the join in 'connection', either reusing an
        existing alias for that join or creating a new one. 'connection' is a
        tuple (lhs, table, lhs_col, col) where 'lhs' is either an existing
        table alias or a table name. The join correspods to the SQL equivalent
        of::

            lhs.lhs_col = table.col

        The 'reuse' parameter can be used in three ways: it can be REUSE_ALL
        which means all joins (matching the connection) are reusable, it can
        be a set containing the aliases that can be reused, or it can be None
        which means a new join is always created.

        If 'promote' is True, the join type for the alias will be LOUTER (if
        the alias previously existed, the join type will be promoted from INNER
        to LOUTER, if necessary).

        If 'outer_if_first' is True and a new join is created, it will have the
        LOUTER join type. Used for example when adding ORed filters, where we
        want to use LOUTER joins except if some other join already restricts
        the join to INNER join.

        A join is always created as LOUTER if the lhs alias is LOUTER to make
        sure we do not generate chains like t1 LOUTER t2 INNER t3.

        If 'nullable' is True, the join can potentially involve NULL values and
        is a candidate for promotion (to "left outer") when combining querysets.
        """
        lhs, table, lhs_col, col = connection
        existing = self.join_map.get(connection, ())
        if reuse == REUSE_ALL:
            reuse = existing
        elif reuse is None:
            reuse = set()
        else:
            reuse = [a for a in existing if a in reuse]
        if reuse:
            alias = reuse[0]
            self.ref_alias(alias)
            if promote or (lhs and self.alias_map[lhs].join_type == self.LOUTER):
                self.promote_joins([alias])
            return alias

        # No reuse is possible, so we need a new alias.
        alias, _ = self.table_alias(table, True)
        if not lhs:
            # Not all tables need to be joined to anything. No join type
            # means the later columns are ignored.
            join_type = None
        elif (promote or outer_if_first
              or self.alias_map[lhs].join_type == self.LOUTER):
            # We need to use LOUTER join if asked by promote or outer_if_first,
            # or if the LHS table is left-joined in the query.
            join_type = self.LOUTER
        else:
            join_type = self.INNER
        join = JoinInfo(table, alias, join_type, lhs, lhs_col, col, nullable)
        self.alias_map[alias] = join
        if connection in self.join_map:
            self.join_map[connection] += (alias,)
        else:
            self.join_map[connection] = (alias,)
        return alias

    def setup_inherited_models(self):
        """
        If the model that is the basis for this QuerySet inherits other models,
        we need to ensure that those other models have their tables included in
        the query.

        We do this as a separate step so that subclasses know which
        tables are going to be active in the query, without needing to compute
        all the select columns (this method is called from pre_sql_setup(),
        whereas column determination is a later part, and side-effect, of
        as_sql()).
        """
        # Skip all proxy models
        opts = self.model._meta.concrete_model._meta
        root_alias = self.tables[0]
        seen = {None: root_alias}

        for field, model in opts.get_fields_with_model():
            if model not in seen:
                link_field = opts.get_ancestor_link(model)
                seen[model] = self.join((root_alias, model._meta.db_table,
                        link_field.column, model._meta.pk.column))
        self.included_inherited_models = seen

    def remove_inherited_models(self):
        """
        Undoes the effects of setup_inherited_models(). Should be called
        whenever select columns (self.select) are set explicitly.
        """
        for key, alias in self.included_inherited_models.items():
            if key:
                self.unref_alias(alias)
        self.included_inherited_models = {}

    def need_force_having(self, q_object):
        """
        Returns whether or not all elements of this q_object need to be put
        together in the HAVING clause.
        """
        for child in q_object.children:
            if isinstance(child, Node):
                if self.need_force_having(child):
                    return True
            else:
                if child[0].split(LOOKUP_SEP)[0] in self.aggregates:
                    return True
        return False

    def add_aggregate(self, aggregate, model, alias, is_summary):
        """
        Adds a single aggregate expression to the Query
        """
        opts = model._meta
        field_list = aggregate.lookup.split(LOOKUP_SEP)
        if len(field_list) == 1 and aggregate.lookup in self.aggregates:
            # Aggregate is over an annotation
            field_name = field_list[0]
            col = field_name
            source = self.aggregates[field_name]
            if not is_summary:
                raise FieldError("Cannot compute %s('%s'): '%s' is an aggregate" % (
                    aggregate.name, field_name, field_name))
        elif ((len(field_list) > 1) or
            (field_list[0] not in [i.name for i in opts.fields]) or
            self.group_by is None or
            not is_summary):
            # If:
            #   - the field descriptor has more than one part (foo__bar), or
            #   - the field descriptor is referencing an m2m/m2o field, or
            #   - this is a reference to a model field (possibly inherited), or
            #   - this is an annotation over a model field
            # then we need to explore the joins that are required.

            field, source, opts, join_list, last, _ = self.setup_joins(
                field_list, opts, self.get_initial_alias(), REUSE_ALL)

            # Process the join chain to see if it can be trimmed
            col, _, join_list = self.trim_joins(source, join_list, last, False)

            # If the aggregate references a model or field that requires a join,
            # those joins must be LEFT OUTER - empty join rows must be returned
            # in order for zeros to be returned for those aggregates.
            self.promote_joins(join_list, True)

            col = (join_list[-1], col)
        else:
            # The simplest cases. No joins required -
            # just reference the provided column alias.
            field_name = field_list[0]
            source = opts.get_field(field_name)
            col = field_name

        # Add the aggregate to the query
        aggregate.add_to_query(self, alias, col=col, source=source, is_summary=is_summary)

    def add_filter(self, filter_expr, connector=AND, negate=False, trim=False,
            can_reuse=None, process_extras=True, force_having=False):
        """
        Add a single filter to the query. The 'filter_expr' is a pair:
        (filter_string, value). E.g. ('name__contains', 'fred')

        If 'negate' is True, this is an exclude() filter. It's important to
        note that this method does not negate anything in the where-clause
        object when inserting the filter constraints. This is because negated
        filters often require multiple calls to add_filter() and the negation
        should only happen once. So the caller is responsible for this (the
        caller will normally be add_q(), so that as an example).

        If 'trim' is True, we automatically trim the final join group (used
        internally when constructing nested queries).

        If 'can_reuse' is a set, we are processing a component of a
        multi-component filter (e.g. filter(Q1, Q2)). In this case, 'can_reuse'
        will be a set of table aliases that can be reused in this filter, even
        if we would otherwise force the creation of new aliases for a join
        (needed for nested Q-filters). The set is updated by this method.

        If 'process_extras' is set, any extra filters returned from the table
        joining process will be processed. This parameter is set to False
        during the processing of extra filters to avoid infinite recursion.
        """
        arg, value = filter_expr
        parts = arg.split(LOOKUP_SEP)
        if not parts:
            raise FieldError("Cannot parse keyword query %r" % arg)

        # Work out the lookup type and remove it from the end of 'parts',
        # if necessary.
        lookup_type = 'exact' # Default lookup type
        num_parts = len(parts)
        if (len(parts) > 1 and parts[-1] in self.query_terms
            and arg not in self.aggregates):
            # Traverse the lookup query to distinguish related fields from
            # lookup types.
            lookup_model = self.model
            for counter, field_name in enumerate(parts):
                try:
                    lookup_field = lookup_model._meta.get_field(field_name)
                except FieldDoesNotExist:
                    # Not a field. Bail out.
                    lookup_type = parts.pop()
                    break
                # Unless we're at the end of the list of lookups, let's attempt
                # to continue traversing relations.
                if (counter + 1) < num_parts:
                    try:
                        lookup_model = lookup_field.rel.to
                    except AttributeError:
                        # Not a related field. Bail out.
                        lookup_type = parts.pop()
                        break

        # By default, this is a WHERE clause. If an aggregate is referenced
        # in the value, the filter will be promoted to a HAVING
        having_clause = False

        # Interpret '__exact=None' as the sql 'is NULL'; otherwise, reject all
        # uses of None as a query value.
        if value is None:
            if lookup_type != 'exact':
                raise ValueError("Cannot use None as a query value")
            lookup_type = 'isnull'
            value = True
        elif callable(value):
            value = value()
        elif isinstance(value, ExpressionNode):
            # If value is a query expression, evaluate it
            value = SQLEvaluator(value, self)
            having_clause = value.contains_aggregate

        for alias, aggregate in self.aggregates.items():
            if alias in (parts[0], LOOKUP_SEP.join(parts)):
                entry = self.where_class()
                entry.add((aggregate, lookup_type, value), AND)
                if negate:
                    entry.negate()
                self.having.add(entry, connector)
                return

        opts = self.get_meta()
        alias = self.get_initial_alias()
        allow_many = trim or not negate

        try:
            field, target, opts, join_list, last, extra_filters = self.setup_joins(
                    parts, opts, alias, can_reuse, allow_many,
                    allow_explicit_fk=True, negate=negate,
                    process_extras=process_extras)
        except MultiJoin as e:
            self.split_exclude(filter_expr, LOOKUP_SEP.join(parts[:e.level]),
                    can_reuse)
            return

        table_promote = False
        join_promote = False

        if (lookup_type == 'isnull' and value is True and not negate and
                len(join_list) > 1):
            # If the comparison is against NULL, we may need to use some left
            # outer joins when creating the join chain. This is only done when
            # needed, as it's less efficient at the database level.
            self.promote_joins(join_list)
            join_promote = True

        # Process the join list to see if we can remove any inner joins from
        # the far end (fewer tables in a query is better).
        nonnull_comparison = (lookup_type == 'isnull' and value is False)
        col, alias, join_list = self.trim_joins(target, join_list, last, trim,
                nonnull_comparison)

        if connector == OR:
            # Some joins may need to be promoted when adding a new filter to a
            # disjunction. We walk the list of new joins and where it diverges
            # from any previous joins (ref count is 1 in the table list), we
            # make the new additions (and any existing ones not used in the new
            # join list) an outer join.
            join_it = iter(join_list)
            table_it = iter(self.tables)
            next(join_it), next(table_it)
            unconditional = False
            for join in join_it:
                table = next(table_it)
                # Once we hit an outer join, all subsequent joins must
                # also be promoted, regardless of whether they have been
                # promoted as a result of this pass through the tables.
                unconditional = (unconditional or
                    self.alias_map[join].join_type == self.LOUTER)
                if join == table and self.alias_refcount[join] > 1:
                    # We have more than one reference to this join table.
                    # This means that we are dealing with two different query
                    # subtrees, so we don't need to do any join promotion.
                    continue
                join_promote = join_promote or self.promote_joins([join], unconditional)
                if table != join:
                    table_promote = self.promote_joins([table])
                # We only get here if we have found a table that exists
                # in the join list, but isn't on the original tables list.
                # This means we've reached the point where we only have
                # new tables, so we can break out of this promotion loop.
                break
            self.promote_joins(join_it, join_promote)
            self.promote_joins(table_it, table_promote or join_promote)

        if having_clause or force_having:
            if (alias, col) not in self.group_by:
                self.group_by.append((alias, col))
            self.having.add((Constraint(alias, col, field), lookup_type, value),
                connector)
        else:
            self.where.add((Constraint(alias, col, field), lookup_type, value),
                connector)

        if negate:
            self.promote_joins(join_list)
            if lookup_type != 'isnull':
                if len(join_list) > 1:
                    for alias in join_list:
                        if self.alias_map[alias].join_type == self.LOUTER:
                            j_col = self.alias_map[alias].rhs_join_col
                            # The join promotion logic should never produce
                            # a LOUTER join for the base join - assert that.
                            assert j_col is not None
                            entry = self.where_class()
                            entry.add(
                                (Constraint(alias, j_col, None), 'isnull', True),
                                AND
                            )
                            entry.negate()
                            self.where.add(entry, AND)
                            break
                if self.is_nullable(field):
                    # In SQL NULL = anyvalue returns unknown, and NOT unknown
                    # is still unknown. However, in Python None = anyvalue is False
                    # (and not False is True...), and we want to return this Python's
                    # view of None handling. So we need to specifically exclude the
                    # NULL values, and because we are inside NOT branch they will
                    # be included in the final resultset. We are essentially creating
                    # SQL like this here: NOT (col IS NOT NULL), where the first NOT
                    # is added in upper layers of the code.
                    self.where.add((Constraint(alias, col, None), 'isnull', False), AND)

        if can_reuse is not None:
            can_reuse.update(join_list)
        if process_extras:
            for filter in extra_filters:
                self.add_filter(filter, negate=negate, can_reuse=can_reuse,
                        process_extras=False)

    def add_q(self, q_object, used_aliases=None, force_having=False):
        """
        Adds a Q-object to the current filter.

        Can also be used to add anything that has an 'add_to_query()' method.
        """
        if used_aliases is None:
            used_aliases = self.used_aliases
        if hasattr(q_object, 'add_to_query'):
            # Complex custom objects are responsible for adding themselves.
            q_object.add_to_query(self, used_aliases)
        else:
            if self.where and q_object.connector != AND and len(q_object) > 1:
                self.where.start_subtree(AND)
                subtree = True
            else:
                subtree = False
            connector = AND
            if q_object.connector == OR and not force_having:
                force_having = self.need_force_having(q_object)
            for child in q_object.children:
                if connector == OR:
                    refcounts_before = self.alias_refcount.copy()
                if force_having:
                    self.having.start_subtree(connector)
                else:
                    self.where.start_subtree(connector)
                if isinstance(child, Node):
                    self.add_q(child, used_aliases, force_having=force_having)
                else:
                    self.add_filter(child, connector, q_object.negated,
                            can_reuse=used_aliases, force_having=force_having)
                if force_having:
                    self.having.end_subtree()
                else:
                    self.where.end_subtree()

                if connector == OR:
                    # Aliases that were newly added or not used at all need to
                    # be promoted to outer joins if they are nullable relations.
                    # (they shouldn't turn the whole conditional into the empty
                    # set just because they don't match anything).
                    self.promote_unused_aliases(refcounts_before, used_aliases)
                connector = q_object.connector
            if q_object.negated:
                self.where.negate()
            if subtree:
                self.where.end_subtree()
        if self.filter_is_sticky:
            self.used_aliases = used_aliases

    def setup_joins(self, names, opts, alias, can_reuse, allow_many=True,
            allow_explicit_fk=False, negate=False, process_extras=True):
        """
        Compute the necessary table joins for the passage through the fields
        given in 'names'. 'opts' is the Options class for the current model
        (which gives the table we are joining to), 'alias' is the alias for the
        table we are joining to. If dupe_multis is True, any many-to-many or
        many-to-one joins will always create a new alias (necessary for
        disjunctive filters). If can_reuse is not None, it's a list of aliases
        that can be reused in these joins (nothing else can be reused in this
        case). Finally, 'negate' is used in the same sense as for add_filter()
        -- it indicates an exclude() filter, or something similar. It is only
        passed in here so that it can be passed to a field's extra_filter() for
        customized behavior.

        Returns the final field involved in the join, the target database
        column (used for any 'where' constraint), the final 'opts' value and the
        list of tables joined.
        """
        joins = [alias]
        last = [0]
        extra_filters = []
        int_alias = None
        for pos, name in enumerate(names):
            last.append(len(joins))
            if name == 'pk':
                name = opts.pk.name
            try:
                field, model, direct, m2m = opts.get_field_by_name(name)
            except FieldDoesNotExist:
                for f in opts.fields:
                    if allow_explicit_fk and name == f.attname:
                        # XXX: A hack to allow foo_id to work in values() for
                        # backwards compatibility purposes. If we dropped that
                        # feature, this could be removed.
                        field, model, direct, m2m = opts.get_field_by_name(f.name)
                        break
                else:
                    names = opts.get_all_field_names() + list(self.aggregate_select)
                    raise FieldError("Cannot resolve keyword %r into field. "
                            "Choices are: %s" % (name, ", ".join(names)))

            if not allow_many and (m2m or not direct):
                for alias in joins:
                    self.unref_alias(alias)
                raise MultiJoin(pos + 1)
            if model:
                # The field lives on a base class of the current model.
                # Skip the chain of proxy to the concrete proxied model
                proxied_model = opts.concrete_model

                for int_model in opts.get_base_chain(model):
                    if int_model is proxied_model:
                        opts = int_model._meta
                    else:
                        lhs_col = opts.parents[int_model].column
                        opts = int_model._meta
                        alias = self.join((alias, opts.db_table, lhs_col,
                                opts.pk.column))
                        joins.append(alias)
            cached_data = opts._join_cache.get(name)
            orig_opts = opts

            if process_extras and hasattr(field, 'extra_filters'):
                extra_filters.extend(field.extra_filters(names, pos, negate))
            if direct:
                if m2m:
                    # Many-to-many field defined on the current model.
                    if cached_data:
                        (table1, from_col1, to_col1, table2, from_col2,
                                to_col2, opts, target) = cached_data
                    else:
                        table1 = field.m2m_db_table()
                        from_col1 = opts.get_field_by_name(
                            field.m2m_target_field_name())[0].column
                        to_col1 = field.m2m_column_name()
                        opts = field.rel.to._meta
                        table2 = opts.db_table
                        from_col2 = field.m2m_reverse_name()
                        to_col2 = opts.get_field_by_name(
                            field.m2m_reverse_target_field_name())[0].column
                        target = opts.pk
                        orig_opts._join_cache[name] = (table1, from_col1,
                                to_col1, table2, from_col2, to_col2, opts,
                                target)

                    int_alias = self.join((alias, table1, from_col1, to_col1),
                            reuse=can_reuse, nullable=True)
                    if int_alias == table2 and from_col2 == to_col2:
                        joins.append(int_alias)
                        alias = int_alias
                    else:
                        alias = self.join(
                                (int_alias, table2, from_col2, to_col2),
                                reuse=can_reuse, nullable=True)
                        joins.extend([int_alias, alias])
                elif field.rel:
                    # One-to-one or many-to-one field
                    if cached_data:
                        (table, from_col, to_col, opts, target) = cached_data
                    else:
                        opts = field.rel.to._meta
                        target = field.rel.get_related_field()
                        table = opts.db_table
                        from_col = field.column
                        to_col = target.column
                        orig_opts._join_cache[name] = (table, from_col, to_col,
                                opts, target)

                    alias = self.join((alias, table, from_col, to_col),
                                      nullable=self.is_nullable(field))
                    joins.append(alias)
                else:
                    # Non-relation fields.
                    target = field
                    break
            else:
                orig_field = field
                field = field.field
                if m2m:
                    # Many-to-many field defined on the target model.
                    if cached_data:
                        (table1, from_col1, to_col1, table2, from_col2,
                                to_col2, opts, target) = cached_data
                    else:
                        table1 = field.m2m_db_table()
                        from_col1 = opts.get_field_by_name(
                            field.m2m_reverse_target_field_name())[0].column
                        to_col1 = field.m2m_reverse_name()
                        opts = orig_field.opts
                        table2 = opts.db_table
                        from_col2 = field.m2m_column_name()
                        to_col2 = opts.get_field_by_name(
                            field.m2m_target_field_name())[0].column
                        target = opts.pk
                        orig_opts._join_cache[name] = (table1, from_col1,
                                to_col1, table2, from_col2, to_col2, opts,
                                target)

                    int_alias = self.join((alias, table1, from_col1, to_col1),
                            reuse=can_reuse, nullable=True)
                    alias = self.join((int_alias, table2, from_col2, to_col2),
                            reuse=can_reuse, nullable=True)
                    joins.extend([int_alias, alias])
                else:
                    # One-to-many field (ForeignKey defined on the target model)
                    if cached_data:
                        (table, from_col, to_col, opts, target) = cached_data
                    else:
                        local_field = opts.get_field_by_name(
                                field.rel.field_name)[0]
                        opts = orig_field.opts
                        table = opts.db_table
                        from_col = local_field.column
                        to_col = field.column
                        # In case of a recursive FK, use the to_field for
                        # reverse lookups as well
                        if orig_field.model is local_field.model:
                            target = opts.get_field_by_name(
                                field.rel.field_name)[0]
                        else:
                            target = opts.pk
                        orig_opts._join_cache[name] = (table, from_col, to_col,
                                opts, target)

                    alias = self.join((alias, table, from_col, to_col),
                            reuse=can_reuse, nullable=True)
                    joins.append(alias)

        if pos != len(names) - 1:
            if pos == len(names) - 2:
                raise FieldError("Join on field %r not permitted. Did you misspell %r for the lookup type?" % (name, names[pos + 1]))
            else:
                raise FieldError("Join on field %r not permitted." % name)

        return field, target, opts, joins, last, extra_filters

    def trim_joins(self, target, join_list, last, trim, nonnull_check=False):
        """
        Sometimes joins at the end of a multi-table sequence can be trimmed. If
        the final join is against the same column as we are comparing against,
        and is an inner join, we can go back one step in a join chain and
        compare against the LHS of the join instead (and then repeat the
        optimization). The result, potentially, involves fewer table joins.

        The 'target' parameter is the final field being joined to, 'join_list'
        is the full list of join aliases.

        The 'last' list contains offsets into 'join_list', corresponding to
        each component of the filter. Many-to-many relations, for example, add
        two tables to the join list and we want to deal with both tables the
        same way, so 'last' has an entry for the first of the two tables and
        then the table immediately after the second table, in that case.

        The 'trim' parameter forces the final piece of the join list to be
        trimmed before anything. See the documentation of add_filter() for
        details about this.

        The 'nonnull_check' parameter is True when we are using inner joins
        between tables explicitly to exclude NULL entries. In that case, the
        tables shouldn't be trimmed, because the very action of joining to them
        alters the result set.

        Returns the final active column and table alias and the new active
        join_list.
        """
        final = len(join_list)
        penultimate = last.pop()
        if penultimate == final:
            penultimate = last.pop()
        if trim and final > 1:
            extra = join_list[penultimate:]
            join_list = join_list[:penultimate]
            final = penultimate
            penultimate = last.pop()
            col = self.alias_map[extra[0]].lhs_join_col
            for alias in extra:
                self.unref_alias(alias)
        else:
            col = target.column
        alias = join_list[-1]
        while final > 1:
            join = self.alias_map[alias]
            if (col != join.rhs_join_col or join.join_type != self.INNER or
                    nonnull_check):
                break
            self.unref_alias(alias)
            alias = join.lhs_alias
            col = join.lhs_join_col
            join_list.pop()
            final -= 1
            if final == penultimate:
                penultimate = last.pop()
        return col, alias, join_list

    def split_exclude(self, filter_expr, prefix, can_reuse):
        """
        When doing an exclude against any kind of N-to-many relation, we need
        to use a subquery. This method constructs the nested query, given the
        original exclude filter (filter_expr) and the portion up to the first
        N-to-many relation field.
        """
        query = Query(self.model)
        query.add_filter(filter_expr)
        query.bump_prefix()
        query.clear_ordering(True)
        query.set_start(prefix)
        # Adding extra check to make sure the selected field will not be null
        # since we are adding a IN <subquery> clause. This prevents the
        # database from tripping over IN (...,NULL,...) selects and returning
        # nothing
        alias, col = query.select[0].col
        query.where.add((Constraint(alias, col, None), 'isnull', False), AND)

        self.add_filter(('%s__in' % prefix, query), negate=True, trim=True,
                can_reuse=can_reuse)

        # If there's more than one join in the inner query (before any initial
        # bits were trimmed -- which means the last active table is more than
        # two places into the alias list), we need to also handle the
        # possibility that the earlier joins don't match anything by adding a
        # comparison to NULL (e.g. in
        # Tag.objects.exclude(parent__parent__name='t1'), a tag with no parent
        # would otherwise be overlooked).
        active_positions = [pos for (pos, count) in
                enumerate(six.itervalues(query.alias_refcount)) if count]
        if active_positions[-1] > 1:
            self.add_filter(('%s__isnull' % prefix, False), negate=True,
                    trim=True, can_reuse=can_reuse)

    def set_limits(self, low=None, high=None):
        """
        Adjusts the limits on the rows retrieved. We use low/high to set these,
        as it makes it more Pythonic to read and write. When the SQL query is
        created, they are converted to the appropriate offset and limit values.

        Any limits passed in here are applied relative to the existing
        constraints. So low is added to the current low value and both will be
        clamped to any existing high value.
        """
        if high is not None:
            if self.high_mark is not None:
                self.high_mark = min(self.high_mark, self.low_mark + high)
            else:
                self.high_mark = self.low_mark + high
        if low is not None:
            if self.high_mark is not None:
                self.low_mark = min(self.high_mark, self.low_mark + low)
            else:
                self.low_mark = self.low_mark + low

    def clear_limits(self):
        """
        Clears any existing limits.
        """
        self.low_mark, self.high_mark = 0, None

    def can_filter(self):
        """
        Returns True if adding filters to this instance is still possible.

        Typically, this means no limits or offsets have been put on the results.
        """
        return not self.low_mark and self.high_mark is None

    def clear_select_clause(self):
        """
        Removes all fields from SELECT clause.
        """
        self.select = []
        self.default_cols = False
        self.select_related = False
        self.set_extra_mask(())
        self.set_aggregate_mask(())

    def clear_select_fields(self):
        """
        Clears the list of fields to select (but not extra_select columns).
        Some queryset types completely replace any existing list of select
        columns.
        """
        self.select = []

    def add_distinct_fields(self, *field_names):
        """
        Adds and resolves the given fields to the query's "distinct on" clause.
        """
        self.distinct_fields = field_names
        self.distinct = True

    def add_fields(self, field_names, allow_m2m=True):
        """
        Adds the given (model) fields to the select set. The field names are
        added in the order specified.
        """
        alias = self.get_initial_alias()
        opts = self.get_meta()

        try:
            for name in field_names:
                field, target, u2, joins, u3, u4 = self.setup_joins(
                        name.split(LOOKUP_SEP), opts, alias, REUSE_ALL,
                        allow_m2m, True)
                final_alias = joins[-1]
                col = target.column
                if len(joins) > 1:
                    join = self.alias_map[final_alias]
                    if col == join.rhs_join_col:
                        self.unref_alias(final_alias)
                        final_alias = join.lhs_alias
                        col = join.lhs_join_col
                        joins = joins[:-1]
                self.promote_joins(joins[1:])
                self.select.append(SelectInfo((final_alias, col), field))
        except MultiJoin:
            raise FieldError("Invalid field name: '%s'" % name)
        except FieldError:
            if LOOKUP_SEP in name:
                # For lookups spanning over relationships, show the error
                # from the model on which the lookup failed.
                raise
            else:
                names = sorted(opts.get_all_field_names() + list(self.extra)
                               + list(self.aggregate_select))
                raise FieldError("Cannot resolve keyword %r into field. "
                                 "Choices are: %s" % (name, ", ".join(names)))
        self.remove_inherited_models()

    def add_ordering(self, *ordering):
        """
        Adds items from the 'ordering' sequence to the query's "order by"
        clause. These items are either field names (not column names) --
        possibly with a direction prefix ('-' or '?') -- or ordinals,
        corresponding to column positions in the 'select' list.

        If 'ordering' is empty, all ordering is cleared from the query.
        """
        errors = []
        for item in ordering:
            if not ORDER_PATTERN.match(item):
                errors.append(item)
        if errors:
            raise FieldError('Invalid order_by arguments: %s' % errors)
        if ordering:
            self.order_by.extend(ordering)
        else:
            self.default_ordering = False

    def clear_ordering(self, force_empty=False):
        """
        Removes any ordering settings. If 'force_empty' is True, there will be
        no ordering in the resulting query (not even the model's default).
        """
        self.order_by = []
        self.extra_order_by = ()
        if force_empty:
            self.default_ordering = False

    def set_group_by(self):
        """
        Expands the GROUP BY clause required by the query.

        This will usually be the set of all non-aggregate fields in the
        return data. If the database backend supports grouping by the
        primary key, and the query would be equivalent, the optimization
        will be made automatically.
        """
        self.group_by = []

        for col, _ in self.select:
            self.group_by.append(col)

    def add_count_column(self):
        """
        Converts the query to do count(...) or count(distinct(pk)) in order to
        get its size.
        """
        if not self.distinct:
            if not self.select:
                count = self.aggregates_module.Count('*', is_summary=True)
            else:
                assert len(self.select) == 1, \
                        "Cannot add count col with multiple cols in 'select': %r" % self.select
                count = self.aggregates_module.Count(self.select[0].col)
        else:
            opts = self.model._meta
            if not self.select:
                count = self.aggregates_module.Count((self.join((None, opts.db_table, None, None)), opts.pk.column),
                                         is_summary=True, distinct=True)
            else:
                # Because of SQL portability issues, multi-column, distinct
                # counts need a sub-query -- see get_count() for details.
                assert len(self.select) == 1, \
                        "Cannot add count col with multiple cols in 'select'."

                count = self.aggregates_module.Count(self.select[0].col, distinct=True)
            # Distinct handling is done in Count(), so don't do it at this
            # level.
            self.distinct = False

        # Set only aggregate to be the count column.
        # Clear out the select cache to reflect the new unmasked aggregates.
        self.aggregates = {None: count}
        self.set_aggregate_mask(None)
        self.group_by = None

    def add_select_related(self, fields):
        """
        Sets up the select_related data structure so that we only select
        certain related models (as opposed to all models, when
        self.select_related=True).
        """
        field_dict = {}
        for field in fields:
            d = field_dict
            for part in field.split(LOOKUP_SEP):
                d = d.setdefault(part, {})
        self.select_related = field_dict
        self.related_select_cols = []

    def add_extra(self, select, select_params, where, params, tables, order_by):
        """
        Adds data to the various extra_* attributes for user-created additions
        to the query.
        """
        if select:
            # We need to pair any placeholder markers in the 'select'
            # dictionary with their parameters in 'select_params' so that
            # subsequent updates to the select dictionary also adjust the
            # parameters appropriately.
            select_pairs = SortedDict()
            if select_params:
                param_iter = iter(select_params)
            else:
                param_iter = iter([])
            for name, entry in select.items():
                entry = force_text(entry)
                entry_params = []
                pos = entry.find("%s")
                while pos != -1:
                    entry_params.append(next(param_iter))
                    pos = entry.find("%s", pos + 2)
                select_pairs[name] = (entry, entry_params)
            # This is order preserving, since self.extra_select is a SortedDict.
            self.extra.update(select_pairs)
        if where or params:
            self.where.add(ExtraWhere(where, params), AND)
        if tables:
            self.extra_tables += tuple(tables)
        if order_by:
            self.extra_order_by = order_by

    def clear_deferred_loading(self):
        """
        Remove any fields from the deferred loading set.
        """
        self.deferred_loading = (set(), True)

    def resolve_basic_deferred_fields(self, field_names):
        """
        Resolves field names into their basic constituents at the end
        point of relationship chains. For each targeted composite field
        this creates new strings with its name replaced by each of its
        constituents.

        This is done each time add_deferred_loading or
        add_immediate_loading is called to make it possible to use
        composite fields as shorthands for their sets of enclosed fields.
        """
        # TODO: This whole method has a big overlap with deferred_to_data.
        # Ideally the whole deferred loading should be refactored to only
        # resolve strings to fields once.
        orig_opts = self.model._meta
        resolved = set()
        for field_name in field_names:
            parts = field_name.split(LOOKUP_SEP)
            cur_model = self.model
            opts = orig_opts
            for name in parts[:-1]:
                old_model = cur_model
                cur_model = opts.get_field_by_name(name)[0].rel.to
                opts = cur_model._meta
            field, model, _, _ = opts.get_field_by_name(parts[-1])
            prefix = LOOKUP_SEP.join(parts[:-1])
            if prefix:
                prefix = prefix + LOOKUP_SEP
            resolved.update(prefix + f.name for f in field.resolve_basic_fields())
        return resolved

    def add_deferred_loading(self, field_names):
        """
        Add the given list of model field names to the set of fields to
        exclude from loading from the database when automatic column selection
        is done. The new field names are added to any existing field names that
        are deferred (or removed from any existing field names that are marked
        as the only ones for immediate loading).
        """
        # Fields on related models are stored in the literal double-underscore
        # format, so that we can use a set datastructure. We do the foo__bar
        # splitting and handling when computing the SQL colum names (as part of
        # get_columns()).
        existing, defer = self.deferred_loading
        field_names = self.resolve_basic_deferred_fields(field_names)
        if defer:
            # Add to existing deferred names.
            self.deferred_loading = existing.union(field_names), True
        else:
            # Remove names from the set of any existing "immediate load" names.
            self.deferred_loading = existing.difference(field_names), False

    def add_immediate_loading(self, field_names):
        """
        Add the given list of model field names to the set of fields to
        retrieve when the SQL is executed ("immediate loading" fields). The
        field names replace any existing immediate loading field names. If
        there are field names already specified for deferred loading, those
        names are removed from the new field_names before storing the new names
        for immediate loading. (That is, immediate loading overrides any
        existing immediate values, but respects existing deferrals.)
        """
        existing, defer = self.deferred_loading
        field_names = set(field_names)
        if 'pk' in field_names:
            field_names.remove('pk')
            field_names.add(self.model._meta.pk.name)
        field_names = self.resolve_basic_deferred_fields(field_names)

        if defer:
            # Remove any existing deferred names from the current set before
            # setting the new names.
            self.deferred_loading = field_names.difference(existing), False
        else:
            # Replace any existing "immediate load" field names.
            self.deferred_loading = field_names, False

    def get_loaded_field_names(self):
        """
        If any fields are marked to be deferred, returns a dictionary mapping
        models to a set of names in those fields that will be loaded. If a
        model is not in the returned dictionary, none of it's fields are
        deferred.

        If no fields are marked for deferral, returns an empty dictionary.
        """
        # We cache this because we call this function multiple times
        # (compiler.fill_related_selections, query.iterator)
        try:
            return self._loaded_field_names_cache
        except AttributeError:
            collection = {}
            self.deferred_to_data(collection, self.get_loaded_field_names_cb)
            self._loaded_field_names_cache = collection
            return collection

    def get_loaded_field_names_cb(self, target, model, fields):
        """
        Callback used by get_deferred_field_names().
        """
        target[model] = set([basic.name for f in fields
                             for basic in f.resolve_basic_fields()])

    def set_aggregate_mask(self, names):
        "Set the mask of aggregates that will actually be returned by the SELECT"
        if names is None:
            self.aggregate_select_mask = None
        else:
            self.aggregate_select_mask = set(names)
        self._aggregate_select_cache = None

    def set_extra_mask(self, names):
        """
        Set the mask of extra select items that will be returned by SELECT,
        we don't actually remove them from the Query since they might be used
        later
        """
        if names is None:
            self.extra_select_mask = None
        else:
            self.extra_select_mask = set(names)
        self._extra_select_cache = None

    def _aggregate_select(self):
        """The SortedDict of aggregate columns that are not masked, and should
        be used in the SELECT clause.

        This result is cached for optimization purposes.
        """
        if self._aggregate_select_cache is not None:
            return self._aggregate_select_cache
        elif self.aggregate_select_mask is not None:
            self._aggregate_select_cache = SortedDict([
                (k,v) for k,v in self.aggregates.items()
                if k in self.aggregate_select_mask
            ])
            return self._aggregate_select_cache
        else:
            return self.aggregates
    aggregate_select = property(_aggregate_select)

    def _extra_select(self):
        if self._extra_select_cache is not None:
            return self._extra_select_cache
        elif self.extra_select_mask is not None:
            self._extra_select_cache = SortedDict([
                (k,v) for k,v in self.extra.items()
                if k in self.extra_select_mask
            ])
            return self._extra_select_cache
        else:
            return self.extra
    extra_select = property(_extra_select)

    def set_start(self, start):
        """
        Sets the table from which to start joining. The start position is
        specified by the related attribute from the base model. This will
        automatically set to the select column to be the column linked from the
        previous table.

        This method is primarily for internal use and the error checking isn't
        as friendly as add_filter(). Mostly useful for querying directly
        against the join table of many-to-many relation in a subquery.
        """
        opts = self.model._meta
        alias = self.get_initial_alias()
        field, col, opts, joins, last, extra = self.setup_joins(
                start.split(LOOKUP_SEP), opts, alias, REUSE_ALL)
        select_col = self.alias_map[joins[1]].lhs_join_col
        select_alias = alias

        # The call to setup_joins added an extra reference to everything in
        # joins. Reverse that.
        for alias in joins:
            self.unref_alias(alias)

        # We might be able to trim some joins from the front of this query,
        # providing that we only traverse "always equal" connections (i.e. rhs
        # is *always* the same value as lhs).
        for alias in joins[1:]:
            join_info = self.alias_map[alias]
            if (join_info.lhs_join_col != select_col
                    or join_info.join_type != self.INNER):
                break
            self.unref_alias(select_alias)
            select_alias = join_info.rhs_alias
            select_col = join_info.rhs_join_col
        self.select = [SelectInfo((select_alias, select_col), None)]
        self.remove_inherited_models()

    def is_nullable(self, field):
        """
        A helper to check if the given field should be treated as nullable.

        Some backends treat '' as null and Django treats such fields as
        nullable for those backends. In such situations field.null can be
        False even if we should treat the field as nullable.
        """
        # We need to use DEFAULT_DB_ALIAS here, as QuerySet does not have
        # (nor should it have) knowledge of which connection is going to be
        # used. The proper fix would be to defer all decisions where
        # is_nullable() is needed to the compiler stage, but that is not easy
        # to do currently.
        if ((connections[DEFAULT_DB_ALIAS].features.interprets_empty_strings_as_nulls)
            and field.empty_strings_allowed):
            return True
        else:
            return field.null

def get_order_dir(field, default='ASC'):
    """
    Returns the field name and direction for an order specification. For
    example, '-foo' is returned as ('foo', 'DESC').

    The 'default' param is used to indicate which way no prefix (or a '+'
    prefix) should sort. The '-' prefix always sorts the opposite way.
    """
    dirn = ORDER_DIR[default]
    if field[0] == '-':
        return field[1:], dirn[1]
    return field, dirn[0]


def setup_join_cache(sender, **kwargs):
    """
    The information needed to join between model fields is something that is
    invariant over the life of the model, so we cache it in the model's Options
    class, rather than recomputing it all the time.

    This method initialises the (empty) cache when the model is created.
    """
    sender._meta._join_cache = {}

signals.class_prepared.connect(setup_join_cache)

def add_to_dict(data, key, value):
    """
    A helper function to add "value" to the set of values for "key", whether or
    not "key" already exists.
    """
    if key in data:
        data[key].add(value)
    else:
        data[key] = set([value])
