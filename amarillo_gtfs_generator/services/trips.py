from ..models.gtfs import GtfsTimeDelta, GtfsStopTime
from amarillo.models.Carpool import MAX_STOPS_PER_TRIP, Carpool, Weekday, StopTime, PickupDropoffType, Driver, RidesharingInfo
from ..gtfs_constants import *
from amarillo.utils.utils import is_older_than_days, yesterday
from shapely.geometry import Point, LineString, box
from geojson_pydantic.geometries import LineString as GeoJSONLineString
from datetime import datetime, timedelta
import os
import json
import logging

logger = logging.getLogger(__name__)

#TODO: remove unused functions from Trip class
class Trip:

    def __init__(self, trip_id, route_name, headsign, url, calendar, departureTime, path, agency, lastUpdated, stop_times, driver: Driver, additional_ridesharing_info: RidesharingInfo, route_color, route_text_color, bbox):
        if isinstance(calendar, set):
            self.runs_regularly = True
            self.weekdays = [ 
                1 if Weekday.monday in calendar else 0,
                1 if Weekday.tuesday in calendar else 0,
                1 if Weekday.wednesday in calendar else 0,
                1 if Weekday.thursday in calendar else 0,
                1 if Weekday.friday in calendar else 0,
                1 if Weekday.saturday in calendar else 0,
                1 if Weekday.sunday in calendar else 0,
            ]
            start_in_day = self._total_seconds(departureTime)
        else:
            self.start = datetime.combine(calendar, departureTime)    
            self.runs_regularly = False
            self.weekdays = [0,0,0,0,0,0,0]

        self.start_time = departureTime
        self.path = path   
        self.trip_id = trip_id
        self.url = url
        self.agency = agency
        self.stops = []
        self.lastUpdated = lastUpdated
        self.stop_times = stop_times
        self.driver = driver
        self.additional_ridesharing_info = additional_ridesharing_info
        self.route_color = route_color
        self.route_text_color = route_text_color
        self.bbox = bbox
        self.route_name = route_name
        self.trip_headsign = headsign

    def path_as_line_string(self):
        return self.path
    
    def _total_seconds(self, instant):
        return instant.hour * 3600 + instant.minute * 60 + instant.second

    def start_time_str(self):
        return self.start_time.strftime("%H:%M:%S")

    def next_trip_dates(self, start_date, day_count=14):
        if self.runs_regularly:
            for single_date in (start_date + timedelta(n) for n in range(day_count)):
                if self.weekdays[single_date.weekday()]==1:
                    yield single_date.strftime("%Y%m%d")
        else:
            yield self.start.strftime("%Y%m%d")

    def route_long_name(self):
        return self.route_name

    def intersects(self, bbox):
        return self.bbox.intersects(box(*bbox))


class TripStore():
    """
    TripStore maintains the currently valid trips. A trip is a
    carpool offer enhanced with all stops this 

    Attributes:
        trips           Dict of currently valid trips.
        deleted_trips   Dict of recently deleted trips.
    """

    def __init__(self, stops_store):
        self.transformer = TripTransformer(stops_store)
        self.stops_store = stops_store
        self.trips = {}
        self.deleted_trips = {}
        self.recent_trips = {}


    def put_carpool(self, carpool: Carpool):
        """
        Adds carpool to the TripStore.
        """
        return self._load_as_trip(carpool)

    def recently_added_trips(self):
        return list(self.recent_trips.values())

    def recently_deleted_trips(self):
        return list(self.deleted_trips.values())

    def _load_carpool_if_exists(self, agency_id: str, carpool_id: str):
        if carpool_exists(agency_id, carpool_id, 'data/enhanced'):
            try:
                return load_carpool(agency_id, carpool_id, 'data/enhanced')
            except Exception as e:
                # An error on restore could be caused by model changes, 
                # in such a case, it need's to be recreated
                logger.warning("Could not restore enhanced trip %s:%s, reason: %s", agency_id, carpool_id, repr(e))

        return None

    def _load_as_trip(self, carpool: Carpool):
        trip = self.transformer.transform_to_trip(carpool)
        id = trip.trip_id
        self.trips[id] = trip
        if not is_older_than_days(carpool.lastUpdated, 1):
            self.recent_trips[id] = trip
        logger.debug("Added trip %s", id)

        return trip

    def delete_carpool(self, agency_id: str, carpool_id: str):
        """
            Deletes carpool from the TripStore.
        """
        agencyScopedCarpoolId = f"{agency_id}:{carpool_id}"
        trip_to_be_deleted = self.trips.get(agencyScopedCarpoolId)
        if trip_to_be_deleted:
            self.deleted_trips[agencyScopedCarpoolId] = trip_to_be_deleted
            del self.trips[agencyScopedCarpoolId]
        
        if self.recent_trips.get(agencyScopedCarpoolId):
            del self.recent_trips[agencyScopedCarpoolId]

        if carpool_exists(agency_id, carpool_id):
            remove_carpool_file(agency_id, carpool_id)
            
        logger.debug("Deleted trip %s", id)

    def unflag_unrecent_updates(self):
        """
        Trips that were last updated before yesterday, are not recent
        any longer. As no updates need to be sent for them any longer,
        they will be removed from recent recent_trips and deleted_trips.
        """
        for key in list(self.recent_trips):
            t = self.recent_trips.get(key)
            if t and t.lastUpdated.date() < yesterday():
                del self.recent_trips[key]

        for key in list(self.deleted_trips):
            t = self.deleted_trips.get(key)
            if t and t.lastUpdated.date() < yesterday():
                del self.deleted_trips[key]


class TripTransformer:

    def __init__(self, stops_store):
        self.stops_store = stops_store

    def transform_to_trip(self, carpool : Carpool):
        stop_times = self._convert_stop_times(carpool)
        route_name = carpool.stops[0].name + " nach " + carpool.stops[-1].name
        headsign= carpool.stops[-1].name
        trip_id = self._trip_id(carpool)
        path = carpool.path
        bbox = box(
            min([pt[0] for pt in path.coordinates]),
            min([pt[1] for pt in path.coordinates]),
            max([pt[0] for pt in path.coordinates]),
            max([pt[1] for pt in path.coordinates]))
            
        trip = Trip(trip_id, route_name, headsign, str(carpool.deeplink), carpool.departureDate, carpool.departureTime, carpool.path, carpool.agency, carpool.lastUpdated, stop_times, carpool.driver, carpool.additional_ridesharing_info, carpool.route_color, carpool.route_text_color, bbox)

        return trip

    def _trip_id(self, carpool):
        return f"{carpool.agency}:{carpool.id}"

    def _convert_stop_times(self, carpool):

        stop_times = [GtfsStopTime(
                self._trip_id(carpool), 
                stop.arrivalTime, 
                stop.departureTime, 
                stop.id, 
                seq_nr+1,
                STOP_TIMES_STOP_TYPE_NONE if stop.pickup_dropoff == PickupDropoffType.only_dropoff else STOP_TIMES_STOP_TYPE_COORDINATE_DRIVER, 
                STOP_TIMES_STOP_TYPE_NONE if stop.pickup_dropoff == PickupDropoffType.only_pickup else STOP_TIMES_STOP_TYPE_COORDINATE_DRIVER, 
                STOP_TIMES_TIMEPOINT_APPROXIMATE) 
            for seq_nr, stop in enumerate(carpool.stops)]
        return stop_times

def load_carpool(agency_id: str, carpool_id: str, folder: str ='data/enhanced') -> Carpool:
    with open(f'{folder}/{agency_id}/{carpool_id}.json', 'r', encoding='utf-8') as f:
        dict = json.load(f)
        carpool = Carpool(**dict)
    return carpool

def carpool_exists(agency_id: str, carpool_id: str, folder: str ='data/enhanced'):
    return os.path.exists(f"{folder}/{agency_id}/{carpool_id}.json")

def remove_carpool_file(agency_id: str, carpool_id: str, folder: str ='data/enhanced'):
    return os.remove(f"{folder}/{agency_id}/{carpool_id}.json")
