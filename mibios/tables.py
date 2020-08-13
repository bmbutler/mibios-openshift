from django.urls import reverse
from django.utils.html import format_html

import django_tables2 as tables

from .models import ChangeRecord, Snapshot


NONE_LOOKUP = 'NULL'


class CountColumn(tables.Column):
    def __init__(self, related_object, view=None, **kwargs):
        url = reverse(
            'queryset_index',
            kwargs=dict(dataset=related_object.name)
        )
        # our (this column's) name
        our_name = related_object.remote_field.name

        if 'linkify' not in kwargs:
            def linkify(record):
                f = {}
                if hasattr(record, 'natural'):
                    # FIXME / TODO: only filter for normnal table, for averaged
                    # tables we need to filter by the average_by fields (to be
                    # implemented)
                    f[our_name] = record.natural

                query = view.build_query_string(filter=f)
                return url + query

            kwargs.update(linkify=linkify)

        # prepare URL for footer
        f = {our_name + '__' + k: v for k, v in view.filter.items()}

        elist = []
        for i in view.excludes:
            e = {our_name + '__' + k: v for k, v in i.items()}
            if e:
                elist.append(e)
        # if there is a filter selecting for us, then skip exclusion of missing
        # data:
        for i in f:
            if i.startswith(our_name):
                break
        else:
            elist.append({our_name: NONE_LOOKUP})

        q = view.build_query_string(filter=f, excludes=elist,
                                    negate=view.negate)
        self.footer_url = url + q

        super().__init__(self, **kwargs)
        # verb name can be set after __init__, setting explicitly before
        # interferes with the automatic column class selection
        self.verbose_name = related_object.name + ' count'

    def render_footer(self, bound_column, table):
        total = 0
        for row in table.data:
            total += bound_column.accessor.resolve(row)
        return format_html('all: <a href={}>{}</a>', self.footer_url, total)


class AverageGroupCountColumn(tables.Column):
    def __init__(self, avg_by=[], view=None, **kwargs):
        self.avg_by = avg_by
        self.view = view
        self.url = reverse(
            'queryset_index',
            kwargs=dict(dataset=view.dataset_name)
        )

        if 'linkify' not in kwargs:
            kwargs.update(linkify=self.linkify)

        super().__init__(self, **kwargs)
        # verb name can be set after __init__, setting explicitly before
        # interferes with the automatic column class selection
        self.verbose_name = 'Avg Group N'

    @property
    def linkify(self):
        """
        Attribut containing the linkify function
        """
        if not hasattr(self, '_linkify_fn'):
            def fn(record):
                f = {}
                for i in self.avg_by:
                    if not i:
                        continue
                    # TODO: won't work if '__' in i, right?
                    f[i] = getattr(record, i)

                query = self.view.build_query_string(filter=f)
                return self.url + query
            self._linkify_fn = fn
        return self._linkify_fn


class ManyToManyColumn(tables.ManyToManyColumn):
    def __init__(self, *args, **kwargs):
        if 'default' not in kwargs:
            kwargs['default'] = ''

        super().__init__(*args, **kwargs)


class Table(tables.Table):
    pass


def table_factory(model=None, field_names=[], view=None, count_columns=True,
                  extra={}):
    """
    Generate table class from list of field/annotation/column names etc.

    :param list field_names: Names of a queryset's fields/annotations/lookups
    :param TableView view: The TableView object, will be passed to e.g.
                           CountColumn which needs various view attributes to
                           generate href urls.
    """

    meta_opts = dict(
        model=model,
        template_name='django_tables2/bootstrap.html',
        fields=[],
    )
    opts = {}

    for i in field_names:
        i = tables.A(i.replace('__', '.'))
        meta_opts['fields'].append(i)

        # make one of id or name columns have an edit link
        if i == 'name':
            sort_kw = {}
            if 'name' not in model.get_fields().names:
                # name is actually the natural property, so have to set
                # some proxy sorting, else the machinery tries to fetch the
                # 'name' column (and fails)
                if model._meta.ordering:
                    sort_kw['order_by'] = model._meta.ordering
                else:
                    sort_kw['order_by'] = None
            opts[i] = tables.Column(linkify=True, **sort_kw)
        elif i == 'id':
            # hide id if name is present
            opts[i] = tables.Column(linkify='name' not in field_names,
                                    visible='name' not in field_names)

        # m2m fields
        elif i in [j.name for j in model._meta.many_to_many]:
            opts[i] = tables.ManyToManyColumn()

        elif i == 'natural':
            opts[i] = tables.Column(orderable=False)

        # averages
        elif i == 'avg_group_count':
            # opts[i] = AverageGroupCountColumn(view=view)
            pass

    if count_columns:
        # reverse relations -> count columns
        for i in model._meta.related_objects:
            opts[i.name + '__count'] = CountColumn(i, view=view)
            meta_opts['fields'].append(i.name + '__count')

    for k, v in extra.items():
        # TODO: allow specifiying the position
        meta_opts.append(k)
        opts[k] = v

    parent = tables.Table
    Meta = type('Meta', (getattr(parent, 'Meta', object),), meta_opts)
    opts.update(Meta=Meta)

    name = 'Autogenerated' + model._meta.model_name.capitalize() + 'Table'
    klass = type(name, (parent, ), opts)
    # TODO: monkey-patching verbose_names?
    return klass


class HistoryTable(tables.Table):
    class Meta:
        model = ChangeRecord
        fields = (
            'timestamp', 'is_created', 'is_deleted', 'user', 'file.file',
            'line', 'command_line', 'fields',
        )


class DeletedHistoryTable(tables.Table):
    record_natural = tables.Column(
        verbose_name='record name',
        linkify=(
            'record_history',
            {
                'dataset': tables.A('record_type.model'),
                'natural': tables.A('record_natural') or tables.A('record_pk'),
            }
        )
    )

    class Meta:
        model = ChangeRecord
        fields = ('timestamp', 'user', 'record_natural',)


class SnapshotListTable(tables.Table):
    """
    Table of database snapshots

    The name of each snapshotlinks to a page listing the tables available for
    that snapshot
    """
    name = tables.Column(linkify=('snapshot', {'name': tables.A('name')}))

    class Meta:
        model = Snapshot
        fields = ('timestamp', 'name', 'note')


class SnapshotTableColumn(tables.Column):
    def __init__(self, snapshot_name, **kwargs):
        def linkify(record):
            return reverse(
                'snapshot_table',
                kwargs=dict(name=snapshot_name, table=record['table'])
            )
        super().__init__(self, linkify=linkify, **kwargs)
        self.verbose_name = 'available tables'
