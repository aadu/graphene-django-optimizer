import functools

from django.core.exceptions import FieldDoesNotExist
from django.db.models import ForeignKey, Prefetch
from django.db.models.constants import LOOKUP_SEP
from graphene.types.resolver import attr_resolver
from graphene_django import DjangoObjectType
from graphene_django.fields import DjangoListField
from graphql import ResolveInfo
from graphql.execution.base import (
    get_field_def,
)
from graphql.language.ast import (
    FragmentSpread,
    InlineFragment,
)
from graphql.type.definition import (
    GraphQLInterfaceType,
    GraphQLUnionType,
)

from .utils import is_iterable


def query(queryset, info):
    return QueryOptimizer(info).optimize(queryset)


class QueryOptimizer(object):
    def __init__(self, info):
        self.root_info = info

    def optimize(self, queryset):
        info = self.root_info
        field_def = get_field_def(info.schema, info.parent_type, info.field_name)
        store = self._optimize_gql_selections(
            self._get_type(field_def),
            info.field_asts[0],
            # info.parent_type,
        )
        return store.optimize_queryset(queryset)

    def _get_type(self, field_def):
        a_type = field_def.type
        if hasattr(a_type, 'of_type'):
            a_type = a_type.of_type
        return a_type

    def _optimize_gql_selections(self, field_type, field_ast):
        store = QueryOptimizerStore()
        selection_set = field_ast.selection_set
        if not selection_set:
            return store
        optimized_fields_by_model = {}
        schema = self.root_info.schema
        graphql_type = schema.get_graphql_type(field_type.graphene_type)
        if isinstance(graphql_type, GraphQLUnionType) or isinstance(graphql_type, GraphQLInterfaceType):
            possible_types = schema.get_possible_types(graphql_type)
        else:
            possible_types = (field_type, )
        for selection in selection_set.selections:
            if isinstance(selection, InlineFragment):
                fragment_type_name = selection.type_condition.name.value
                fragment_type = schema.get_type(fragment_type_name)
                fragment_model = fragment_type.graphene_type._meta.model
                parent_model = possible_types[0].graphene_type._meta.model
                path_from_parent = fragment_model._meta.get_path_from_parent(parent_model)
                select_related_name = LOOKUP_SEP.join(p.join_field.name for p in path_from_parent)
                if select_related_name:
                    fragment_store = self._optimize_gql_selections(
                        fragment_type,
                        selection,
                        # parent_type,
                    )
                    store.select_related(select_related_name, fragment_store)
            else:
                name = selection.name.value
                if isinstance(selection, FragmentSpread):
                    fragment = self.root_info.fragments[name]
                    fragment_store = self._optimize_gql_selections(
                        field_type,
                        fragment,
                        # parent_type,
                    )
                    store.append(fragment_store)
                else:
                    for possible_type in possible_types:
                        selection_field_def = possible_type.fields.get(name)
                        if selection_field_def:
                            model = possible_type.graphene_type._meta.model
                            field_model = optimized_fields_by_model.setdefault(name, model)
                            if field_model == model:
                                self._optimize_field(
                                    store,
                                    model,
                                    selection,
                                    selection_field_def,
                                    possible_type,
                                )
        return store

    def _optimize_field(self, store, model, selection, field_def, parent_type):
        optimized = self._optimize_field_by_name(store, model, selection, field_def)
        optimized = self._optimize_field_by_hints(store, selection, field_def, parent_type) or optimized
        if not optimized:
            store.abort_only_optimization()

    def _optimize_field_by_name(self, store, model, selection, field_def):
        name = self._get_name_from_resolver(field_def.resolver)
        if not name:
            return False
        model_field = self._get_model_field_from_name(model, name)
        if not model_field:
            return False
        if self._is_foreign_key_id(model_field, name):
            store.only(name)
            return True
        if model_field.many_to_one or model_field.one_to_one:
            field_store = self._optimize_gql_selections(
                self._get_type(field_def),
                selection,
                # parent_type,
            )
            store.select_related(name, field_store)
            return True
        if model_field.one_to_many or model_field.many_to_many:
            field_store = self._optimize_gql_selections(
                self._get_type(field_def),
                selection,
                # parent_type,
            )
            related_queryset = model_field.related_model.objects.all()
            store.prefetch_related(name, field_store, related_queryset)
            return True
        if not model_field.is_relation:
            store.only(name)
            return True
        return False

    def _get_optimization_hints(self, resolver):
        return getattr(resolver, 'optimization_hints', None)

    def _optimize_field_by_hints(self, store, selection, field_def, parent_type):
        optimization_hints = self._get_optimization_hints(field_def.resolver)
        if not optimization_hints:
            return False
        info = self._create_resolve_info(
            selection.name.value,
            (selection,),
            self._get_type(field_def),
            parent_type,
        )
        args = tuple(arg.value.value for arg in selection.arguments)
        self._add_optimization_hints(
            optimization_hints.select_related(info, *args),
            store.select_list,
        )
        self._add_optimization_hints(
            optimization_hints.prefetch_related(info, *args),
            store.prefetch_list,
        )
        if store.only_list is not None:
            self._add_optimization_hints(
                optimization_hints.only(info, *args),
                store.only_list,
            )
        return True

    def _add_optimization_hints(self, source, target):
        if source:
            if not is_iterable(source):
                source = (source,)
            target += source

    def _get_name_from_resolver(self, resolver):
        optimization_hints = self._get_optimization_hints(resolver)
        if optimization_hints:
            name = optimization_hints.model_field
            if name:
                return name
        if resolver == DjangoObjectType.resolve_id:
            return 'id'
        elif isinstance(resolver, functools.partial):
            resolver_fn = resolver
            if resolver_fn.func == DjangoListField.list_resolver:
                resolver_fn = resolver_fn.args[0]
            if resolver_fn.func == attr_resolver:
                return resolver_fn.args[0]

    def _get_model_field_from_name(self, model, name):
        try:
            return model._meta.get_field(name)
        except FieldDoesNotExist:
            descriptor = model.__dict__.get(name)
            if not descriptor:
                return None
            return getattr(descriptor, 'rel', None) \
                or getattr(descriptor, 'related', None)  # Django < 1.9

    def _is_foreign_key_id(self, model_field, name):
        return (
            isinstance(model_field, ForeignKey) and
            model_field.name != name and
            model_field.get_attname() == name
        )

    def _create_resolve_info(self, field_name, field_asts, return_type, parent_type):
        return ResolveInfo(
            field_name,
            field_asts,
            return_type,
            parent_type,
            schema=self.root_info.schema,
            fragments=self.root_info.fragments,
            root_value=self.root_info.root_value,
            operation=self.root_info.operation,
            variable_values=self.root_info.variable_values,
            context=self.root_info.context,
        )


class QueryOptimizerStore():
    def __init__(self):
        self.select_list = []
        self.prefetch_list = []
        self.only_list = []

    def select_related(self, name, store):
        if store.select_list:
            for select in store.select_list:
                self.select_list.append(name + LOOKUP_SEP + select)
        else:
            self.select_list.append(name)
        for prefetch in store.prefetch_list:
            if isinstance(prefetch, Prefetch):
                prefetch.add_prefix(name)
            else:
                prefetch = name + LOOKUP_SEP + prefetch
            self.prefetch_list.append(prefetch)
        if self.only_list is not None:
            if store.only_list is None:
                self.abort_only_optimization()
            else:
                for only in store.only_list:
                    self.only_list.append(name + LOOKUP_SEP + only)

    def prefetch_related(self, name, store, queryset):
        if store.select_list or store.only_list:
            queryset = store.optimize_queryset(queryset)
            self.prefetch_list.append(Prefetch(name, queryset=queryset))
        elif store.prefetch_list:
            for prefetch in store.prefetch_list:
                self.prefetch_list.append(name + LOOKUP_SEP + prefetch)
        else:
            self.prefetch_list.append(name)

    def only(self, field):
        if self.only_list is not None:
            self.only_list.append(field)

    def abort_only_optimization(self):
        self.only_list = None

    def optimize_queryset(self, queryset):
        for select in self.select_list:
            queryset = queryset.select_related(select)
        for prefetch in self.prefetch_list:
            queryset = queryset.prefetch_related(prefetch)
        if self.only_list:
            queryset = queryset.only(*self.only_list)
        return queryset

    def append(self, store):
        self.select_list += store.select_list
        self.prefetch_list += store.prefetch_list
        if store.only_list is None:
            self.only_list = None
        else:
            self.only_list += store.only_list
