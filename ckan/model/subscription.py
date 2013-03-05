import ckan.plugins as p
import datetime
import domain_object
import meta
import sqlalchemy as sa
import types as _types

__all__ = ['Subscription', 'SubscriptionItem']



class Subscription(domain_object.DomainObject):
    '''
        Describes a user interest in a general way.
        For instance a user could subscribe to a search in order to receive
        notifications when a new dataset that matches the search criteria
        is published.

        This domain object stores a name, owner and a definition (in case
        of a search the search criteria).
        
        When evaluating a subscription's definition, new entities that
        fulfill the definition could show up and other could disappear.
        In order to be able to detect them, subscription items will be stored
        separately.
    '''
    
    def __init__(self, name = None, definition = None, owner_id = None):
        self.id = _types.make_uuid()
        self.definition = definition
        self.name = name
        self.owner_id = owner_id
        self.last_evaluated = datetime.datetime.now()
        self.last_modified = datetime.datetime.now()


    def get_item_list(self):
        query = meta.Session.query(SubscriptionItem)
        query = query.filter(SubscriptionItem.subscription_id == self.id)
        return query.all()


    def subscribed_objects(self):
        type_ = self.definition['type']

        objects = []
        if type_ == 'search':
            import ckan.model as model
            for item in self.get_item_list():
                dataset = model.Package.get(item.key)
                if dataset:
                    objects.append(dataset)
        else:
            for plugin in p.PluginImplementations(p.ISubscription):
                if plugin.is_responsible(self.definition):
                    for item in self.get_item_list():
                        objects.extend(plugin.get_objects_from_item(item))
                    break
        return objects


    def get_updates_count(self):
        count = 0
        item_list = self.get_item_list()
        for item in item_list:
            if item.flag in ['changed', 'added']:
                count += 1
        return count


    def update_item_list_when_necessary(self, context, timespan_after_last_update):
        if self.last_evaluated > datetime.datetime.now() - datetime.timedelta(minutes=timespan_after_last_update):
            return

        type_ = self.definition['type']

        if type_ == 'search':
            import ckan.lib.base as base
            import ckan.logic.action.get as get
            search_dict = {
                'q': self.definition['query'],
                'filters': self.definition['filters'],
                'facet.field': base.g.facets,
                'rows': 50,
                'start': 0,
                'sort': 'metadata_modified desc',
                'extras': self.definition['extras']
            }
            results = get.package_search(context, search_dict)['results']
            data_list = [{'id': result['id'], 'modified': result['metadata_modified']} for result in results]
            key_name = 'id'
        else:
            for plugin in p.PluginImplementations(p.ISubscription):
                if plugin.is_responsible(self.definition):
                    data_list, key_name = plugin.get_current_items(self.definition)
                    break

        self.update_item_list(data_list, key_name)

        if not context.get('defer_commit'):
            context['model'].repo.commit()


    def update_item_list(self, data_by_definition, key_name):
        self.last_evaluated = datetime.datetime.now()

        self._prepare_items()
        self._prepare_data_by_definition(data_by_definition, key_name)

        self._determine_added_items()
        self._determine_removed_items()
        self._determine_changed_items()
        self._determine_remaining_items()

        self._save_items()


    def mark_item_list_changes_as_seen(self):
        self._item_list = self.get_item_list()
        self._delete_removed_items()
        self._set_flag_to_seen()
        self._save_items()


    def _prepare_items(self):
        self._item_list = self.get_item_list()
        self._item_dict = dict([(item.key, item) for item in self._item_list])
        self._item_ids = set(self._item_dict.keys())


    def _prepare_data_by_definition(self, data_by_definition, key_name):
        if isinstance(data_by_definition, dict):
            self._item_data_dict_by_definition = data_by_definition
        elif isinstance(data_by_definition, list):
            self._item_data_dict_by_definition = {}
            for item_data in data_by_definition:
               self._item_data_dict_by_definition[self._get_key(item_data, key_name)] = item_data

        self._item_ids_by_definition = set(self._item_data_dict_by_definition.keys())


    def _get_key(self, item_data, key_name):
        if key_name:
            return item_data[key_name]
        return unicode(_hash(item_data))


    def _determine_added_items(self):
        self._added_item_ids = self._item_ids_by_definition - self._item_ids
        for item_id in self._added_item_ids:
            self._item_list.append(
                SubscriptionItem(subscription_id=self.id,
                                 key=item_id,
                                 data=self._item_data_dict_by_definition[item_id],
                                 flag='added'))


    def _determine_removed_items(self):
        self._removed_item_ids = self._item_ids - self._item_ids_by_definition
        for item_id in self._removed_item_ids:
            self._item_dict[item_id].flag='removed'


    def _determine_changed_items(self):
        self._remaining_item_ids = self._item_ids & self._item_ids_by_definition

        for item_id in self._remaining_item_ids:
            item_data = self._item_dict[item_id].data
            item_data_by_definition = self._item_data_dict_by_definition[item_id]
            if not self._item_data_equal_item_data(item_data, item_data_by_definition):
                self._item_dict[item_id].data = item_data_by_definition
                self._item_dict[item_id].flag='changed'


    def _item_data_equal_item_data(self, item_data, equal_item_data):
        if set(item_data) ^ set(equal_item_data):
            return False
        for key, value in item_data.iteritems():
            if value != equal_item_data[key]:
                return False
        return True


    def _determine_remaining_items(self):
        self._remaining_item_ids = self._item_ids & self._item_ids_by_definition


    def _delete_removed_items(self):
        self._item_list = [item for item in self._item_list if item.flag != 'removed']
        query = meta.Session.query(SubscriptionItem)
        query = query.filter(SubscriptionItem.subscription_id == self.id)
        query = query.filter(SubscriptionItem.flag == 'removed')
        query.delete()


    def _set_flag_to_seen(self):
        for item in self._item_list:
            item.flag = 'seen'


    def _save_items(self):
        meta.Session.add_all(self._item_list)


class SubscriptionItem(domain_object.DomainObject):
    '''
        Every subscription results in a dynamic list of objects that
        match the definition of the corresponding subscription. In order
        to keep track of each of them and to limit the re-evaluations of
        the subscription definition, a list of these objects is saved.

        id - unique identifier of the item
        subscripton_id - foreign key to the corresponding subscription
        key - for identified a item with the list of all items of a subscription
              [optional] the foreign key to a domain object (like packages)
        data - additional fields that represented several attributes of the object
        flag - one of ['seen', 'changed', 'added', 'removed']
    '''
    def __init__(self, subscription_id, key, data, flag):
        self.id = _types.make_uuid()
        self.subscription_id = subscription_id
        self.key = key
        if data is None:
            self.data = {}
        else:
            self.data = data
        self.flag = flag


subscription_table = sa.Table(
    'subscription', meta.metadata,
    sa.Column('id', sa.types.UnicodeText, primary_key=True, default=_types.make_uuid),
    sa.Column('definition', _types.JsonDictType, nullable=False),
    sa.Column('name', sa.types.UnicodeText, nullable=False),
    sa.Column('owner_id', sa.types.UnicodeText, sa.ForeignKey('user.id', onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    sa.Column('last_evaluated', sa.types.DateTime, nullable=False),
    sa.Column('last_modified', sa.types.DateTime, nullable=False),
    )

subscription_item_table = sa.Table(
    'subscription_item', meta.metadata,
    sa.Column('id', sa.types.UnicodeText, primary_key=True, default=_types.make_uuid),
    sa.Column('subscription_id', sa.types.UnicodeText, sa.ForeignKey('subscription.id', onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
    sa.Column('key', sa.types.UnicodeText, nullable=False),
    sa.Column('data', _types.JsonDictType),
    sa.Column('flag', sa.types.Boolean, nullable=False),
    )

meta.mapper(Subscription, subscription_table)
meta.mapper(SubscriptionItem, subscription_item_table)


def _hash(object_):
    if isinstance(object_, list) or isinstance(object_, set):
        return hash(tuple( [_hash(element) for element in object_] ))
    elif isinstance(object_, dict):
        return hash(tuple( [(hash(key), _hash(value)) for (key, value) in object_.items()] ))
    return hash(object_)


def get_subscription(user_id, data_dict):
    '''
        Return a subscription object by
            (user, subscription_id) or
            (user, subscription_name) or
            (user, subscription_definition)
    '''
    import ckan.model as model

    if 'subscription_id' in data_dict:
        id_ = data_dict['subscription_id']
        query = model.Session.query(model.Subscription)
        query = query.filter(model.Subscription.id==id_)
        subscription = query.first()
        if subscription.owner_id != user_id:
            return None
    elif 'subscription_name' in data_dict:
        name = data_dict['subscription_name']
        query = model.Session.query(model.Subscription)
        query = query.filter(model.Subscription.owner_id==user_id)
        query = query.filter(model.Subscription.name==name)
        subscription = query.first() 
    elif 'subscription_definition' in data_dict:
        definition = data_dict['subscription_definition']
        query = model.Session.query(model.Subscription)
        query = query.filter(model.Subscription.owner_id==user_id)
        subscription = None
        for row in query.all():
            if is_subscription_equal_definition(row, definition):
                subscription = row
                break
    else:
        return None
    return subscription


def is_subscription_equal_definition(subscription, definition):
    if subscription.definition['type'] != definition['type']:
        return False

    if definition['type'] == 'search':
        if subscription.definition['query'] != definition['query']:
            return False
        if _hash(subscription.definition['filters']) != _hash(definition['filters']):
            return False
        if _hash(subscription.definition['extras']) != _hash(definition['extras']):
            return False
        return True
    else:
        for plugin in p.PluginImplementations(p.ISubscription):
            if plugin.is_responsible(definition):
                return plugin.is_subscription_equal_definition(subscription, definition)
    return False
