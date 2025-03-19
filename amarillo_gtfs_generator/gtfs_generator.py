from fastapi import FastAPI, Body, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse

from .gtfs_export import GtfsExport, GtfsFeedInfo, GtfsAgency
from .gtfs import GtfsRtProducer
from amarillo.utils.container import container
# from amarillo.plugins.gtfs_export.router import router
# from amarillo.plugins.enhancer.configuration import configure_enhancer_services
from glob import glob
import json
import schedule
import threading
import time
import logging
import os
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from amarillo.models.Carpool import Carpool
from .utils import _assert_region_exists
from amarillo_stops import stops
from .services.trips import TripStore
from .services.carpools import CarpoolService
from .services.agencies import AgencyService
from .services.regions import RegionService
from .services.config import config
from amarillo.utils.utils import agency_carpool_ids_from_filename


logger = logging.getLogger(__name__)

class EventHandler(FileSystemEventHandler):

    def on_modified(self, event):        
        if not event.src_path.endswith(".json"):
            return

        logger.info("MODIFIED: Created %s", event.src_path)
        try:
            with open(event.src_path, 'r', encoding='utf-8') as f:
                dict = json.load(f)
                carpool = Carpool(**dict)

            container['carpools'].put(carpool.agency, carpool.id, carpool)
        except FileNotFoundError as e:
            logger.error("Carpool could not be added, as already deleted (%s)", event.src_path)
        except:
            logger.exception("Eventhandler on_closed encountered exception")        

    def on_deleted(self, event):
        try:
            logger.info("DELETE: Removing %s", event.src_path)
            (agency_id, carpool_id) = agency_carpool_ids_from_filename(event.src_path)
            container['carpools'].delete(agency_id, carpool_id)
        except:
            logger.exception("Eventhandler on_deleted encountered exception")



def init():
	logger.info(f"Current working directory is {os.path.abspath(os.getcwd())}")
	if not os.path.isdir('data/agency'):
		logger.error(f'{os.path.abspath("data/agency")} directory does not exist')
	

	container['agencies'] = AgencyService()
	logger.info("Loaded %d agencies", len(container['agencies'].agencies))

	container['regions'] = RegionService()
	logger.info("Loaded %d regions", len(container['regions'].regions))


	logger.info("Load stops from %s", config.stop_sources_file)
	with open(config.stop_sources_file) as stop_sources_file:
		stop_sources = json.load(stop_sources_file)
		stop_store = stops.StopsStore(stop_sources)

	stop_store.load_stop_sources()
	# TODO: do we need container?
	container['stops_store'] = stop_store
	container['trips_store'] = TripStore(stop_store)

	# TODO: the carpool service may be obsolete
	container['carpools'] = CarpoolService(container['trips_store'], config.max_age_carpool_offers_in_days)

	logger.info("Restore carpools...")

	for agency_id in container['agencies'].agencies:
		for carpool_file_name in glob(f'data/enhanced/{agency_id}/*.json'):
			try:
				with open(carpool_file_name) as carpool_file:
					carpool = Carpool(**(json.load(carpool_file)))
					#TODO: convert to trip and add to tripstore directly
					container['carpools'].put(carpool.agency, carpool.id, carpool)
					logger.info(f"Restored carpool {carpool_file_name}")
			except Exception as e:
				logger.warning("Issue during restore of carpool %s: %s", carpool_file_name, repr(e))

	observer = Observer()  # Watch Manager

	observer.schedule(EventHandler(), 'data/enhanced', recursive=True)
	observer.start()
	start_schedule()

	generate_gtfs()


def run_schedule():
	while 1:
		try:
			schedule.run_pending()
		except Exception as e:
			logger.exception(e)
		time.sleep(1)

def midnight():
	container['stops_store'].load_stop_sources()
	container['trips_store'].unflag_unrecent_updates()
	container['carpools'].purge_outdated_offers()

	generate_gtfs()

#TODO: generate for a specific region only
#TODO: what happens when there are no trips?
def generate_gtfs():
	logger.info("Generate GTFS")

	for region in container['regions'].regions.values():
		# TODO make feed producer infos configurable
		feed_info = GtfsFeedInfo('mfdz', 'MITFAHR|DE|ZENTRALE', 'http://www.mitfahrdezentrale.de', 'de', 1)
		exporter = GtfsExport(
			container['agencies'].agencies,
			feed_info, 
			container['trips_store'], 
			container['stops_store'], 
			region.bbox)
		exporter.export(f"data/gtfs/amarillo.{region.id}.gtfs.zip", "data/tmp/")

def generate_gtfs_rt():
	logger.info("Generate GTFS-RT")
	producer = GtfsRtProducer(container['trips_store'])
	for region in container['regions'].regions.values():
		rt = producer.export_feed(time.time(), f"data/gtfs/amarillo.{region.id}.gtfsrt", bbox=region.bbox)

def start_schedule():
	schedule.every().day.at("00:00").do(midnight)
# 	schedule.every(60).seconds.do(generate_gtfs_rt)
	# Create all feeds once at startup
	# schedule.run_all()
	job_thread = threading.Thread(target=run_schedule, daemon=True)
	job_thread.start()


logging.config.fileConfig('logging.conf', disable_existing_loggers=False)
logger = logging.getLogger("gtfs-generator")

#TODO: clean up metadata
app = FastAPI(title="Amarillo GTFS Generator",
              description="This service allows carpool agencies to publish "
                          "their trip offers, so routing services may suggest "
                          "them as trip options. For carpool offers, only the "
                          "minimum required information (origin/destination, "
                          "optionally intermediate stops, departure time and a "
                          "deep link for booking/contacting the driver) needs to "
                          "be published, booking/contact exchange is to be "
                          "handled by the publishing agency.",
              version="0.0.1",
              # TODO 404
              terms_of_service="http://mfdz.de/carpool-hub-terms/",
              contact={
                  # "name": "unused",
                  # "url": "http://unused",
                  "email": "info@mfdz.de",
              },
              license_info={
                  "name": "AGPL-3.0 License",
                  "url": "https://www.gnu.org/licenses/agpl-3.0.de.html",
              },
              openapi_tags=[
                  {
                      "name": "carpool",
                      # "description": "Find out more about Amarillo - the carpooling intermediary",
                      "externalDocs": {
                          "description": "Find out more about Amarillo - the carpooling intermediary",
                          "url": "https://github.com/mfdz/amarillo",
                      },
                  }],
              redoc_url=None
              )

init()

@app.get("/region/{region_id}/gtfs", 
    summary="Return GTFS Feed for this region",
    response_description="GTFS-Feed (zip-file)",
    response_class=FileResponse,
    responses={
                status.HTTP_404_NOT_FOUND: {"description": "Region not found"},
        }
    )
async def get_file(region_id: str):
	_assert_region_exists(region_id)
	generate_gtfs()
	# verify_permission("gtfs", requesting_user)
	return FileResponse(f'data/gtfs/amarillo.{region_id}.gtfs.zip')

@app.get("/region/{region_id}/gtfs-rt/",
    summary="Return GTFS-RT Feed for this region",
    response_description="GTFS-RT-Feed",
    response_class=FileResponse,
    responses={
                status.HTTP_404_NOT_FOUND: {"description": "Region not found"},
                status.HTTP_400_BAD_REQUEST: {"description": "Bad request, e.g. because format is not supported, i.e. neither protobuf nor json."}
        }
    )
async def get_file(region_id: str, format: str = 'protobuf'):
    generate_gtfs_rt()
    _assert_region_exists(region_id)
    if format == 'json':
        return FileResponse(f'data/gtfs/amarillo.{region_id}.gtfsrt.json')
    elif format == 'protobuf':
        return FileResponse(f'data/gtfs/amarillo.{region_id}.gtfsrt.pbf')
    else:
        message = "Specified format is not supported, i.e. neither protobuf nor json."
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

@app.post("/sync",
	operation_id="sync")
async def post_sync():

	logger.info(f"Sync")

	midnight()
	
@app.get("/", include_in_schema=False)
async def docs_redirect():
    return RedirectResponse(url='/docs')