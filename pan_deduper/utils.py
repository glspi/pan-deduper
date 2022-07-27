import asyncio
import importlib
import importlib.resources as pkg_resources
import json
import logging
import sys
from itertools import combinations
from typing import Any, Dict, List

from lxml import etree
from rich.pretty import pprint

from pan_deduper.panorama_api import Panorama_api

# Logging setup:
logger = logging.getLogger("utils")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s:%(levelname)s:%(message)s")
try:
    file_handler = logging.FileHandler("deduper.log")
except PermissionError:
    print("Permission denied creating deduper.log, check folder permissions.")
    sys.exit(1)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Import 'settings' at runtime
try:
    settings = importlib.import_module("settings")
except (ImportError, ModuleNotFoundError):
    print("\nsettings.py not found!\n")
    settingsfile = pkg_resources.read_text("pan_deduper", "settings.py")
    try:
        with open("settings.py", "w") as f:
            f.write(settingsfile)
    except IOError as e:
        print("Error creating settings.py in local directory, permissions issue?")
        sys.exit(1)
    print(
        "Default settings created in local directory, please review 'settings.py' and run again.\n"
    )
    sys.exit(0)


def get_objects_xml(config, object_type):
    """
    Get objects from xml file instead of Panorama

    Args:
        config: xml config string
        object_type: address/group/services/groups
    Returns:
         Dict/list of objects
    Raises:
        N/A
    """
    my_objs = {}
    for dg in settings.device_groups:
        object_xpath = None
        if object_type == "addresses":
            object_xpath = f"//devices/entry[@name='localhost.localdomain']/device-group/entry[@name='{dg}']/address/entry"
        if object_type == "address-groups":
            object_xpath = f"//devices/entry[@name='localhost.localdomain']/device-group/entry[@name='{dg}']/address-group/entry"
        if object_type == "services":
            object_xpath = f"//devices/entry[@name='localhost.localdomain']/device-group/entry[@name='{dg}']/service/entry"
        if object_type == "service-groups":
            object_xpath = f"//devices/entry[@name='localhost.localdomain']/device-group/entry[@name='{dg}']/service-group/entry"

        objs = config.xpath(object_xpath)

        if not objs:
            print(f"No {object_type} found in {dg}, moving on...")
            my_objs[dg] = set([])
            continue

        my_objs[dg] = set([name.get("name") for name in objs])

    return {object_type: my_objs}


async def get_objects(pan: Panorama_api, object_type: str, names_only: bool = True):
    """
    Get objects from Panorama API

    Args:
        pan:    Panorama API Object
        object_type:    address/group/service/group
        names_only: return only the names or the full object
    Returns:
         Dict/List of objects
    Raises:
        N/A
    """
    my_objs = {}
    for dg in settings.device_groups:
        # Get objects
        objs = await pan.get_objects(object_type=object_type, device_group=dg)

        if not objs:
            print(f"No {object_type} found in {dg}, moving on...")
            my_objs[dg] = set([])
            continue

        if names_only:
            my_objs[dg] = set(
                [
                    name["@name"]
                    for name in objs
                    if name["@loc"] not in settings.exclude_device_groups
                ]  # Global/parent DG's objects still show up
            )  # We only care about the names, not values
        else:
            my_objs[dg] = [
                obj for obj in objs if obj["@loc"] not in settings.exclude_device_groups
            ]

    return {object_type: my_objs}


def find_duplicates(my_objects):
    """
    Finds the duplicate objects (multiple device groups contain the object)

    Args:
     my_objects: list of objects to search through
    Returns:
        duplicates: Dict of duplicate object names containing list of device-groups
    Raises:
        N/A
    """
    duplicates = {}
    for items in combinations(my_objects, r=2):
        dupes = my_objects[items[0]].intersection(my_objects[items[1]])

        for obj in dupes:
            if duplicates.get(obj):
                if items[0] not in duplicates[obj]:
                    duplicates[obj].append(items[0])
                if items[1] not in duplicates[obj]:
                    duplicates[obj].append(items[1])
            else:
                duplicates[obj] = list(items)

    return duplicates


async def set_device_groups(*, config=None, pan: Panorama_api = None):
    """
    Set the device groups that will be searched through

    Args:
        only 1 of below should be provided
        config: xml config string (if provided)
        pan: panorama object (if provided)
    Returns:
        N/A
    Raises:
         N/A
    """
    if config:
        if not settings.device_groups:
            dgs = config.find(
                "devices/entry[@name='localhost.localdomain']/device-group"
            )
            for entry in dgs.getchildren():
                settings.device_groups.append(entry.get("name"))
    else:
        if not settings.device_groups:
            settings.device_groups = await pan.get_device_groups()

    if settings.exclude_device_groups:
        for dg in settings.exclude_device_groups:
            if dg in settings.device_groups:
                settings.device_groups.remove(dg)

    print(f"Comparing these device groups:\n\t{settings.device_groups}")
    print(f"and these object types:\n\t{settings.to_dedupe}")


async def run_deduper(
    *,
    configstr: str = None,
    panorama: str = None,
    username: str = None,
    password: str = None,
) -> None:
    """
    Main program

    Args:
        configstr:  xml config file string
        panorama:   panorama IP/FQDN
        username:   panorama username
        password:   panorama password
    Returns:
        N/A

    Raises:
        N/A
    """
    logger.info("")
    logger.info("----Running deduper---")
    logger.info("")

    if configstr:
        config = etree.fromstring(configstr)
        await set_device_groups(config=config)
        my_objs = [
            get_objects_xml(config, object_type) for object_type in settings.to_dedupe
        ]

    else:
        pan = Panorama_api(panorama=panorama, username=username, password=password)
        await pan.login()
        await set_device_groups(pan=pan)
        coroutines = [
            get_objects(pan, object_type, names_only=True)
            for object_type in settings.to_dedupe
        ]
        my_objs = await asyncio.gather(*coroutines)

    # Comment the black magic
    results = {}
    for obj in my_objs:
        (object_type,) = obj.keys()  # Fancy way to get whatever the only key is
        duplicates = find_duplicates(obj[object_type])  # Get duplicates
        results[object_type] = {}

        # Only duplicates that meet 'minimum' count
        for dupe, dgs in duplicates.items():
            if len(dgs) >= settings.minimum_duplicates:
                results[object_type].update({dupe: dgs})

    write_output(results)
    print("Duplicates found: \n")
    pprint(results)

    if settings.push_to_panorama and not configstr:
        yesno = ""
        while yesno not in ("y", "n", "yes", "no"):
            yesno = input("About to begin moving duplicate objects...continue? (y/n): ")
        if yesno == "yes" or yesno == "y":
            await push_to_panorama(pan=pan, results=results)
        else:
            print("Done! Output above also saved in duplicates.json.")
    else:
        print("Done! Output above also saved in duplicates.json.")


async def push_to_panorama(pan, results) -> None:
    """
    Push the changes to Panorama

    Delete objects from local device groups
    Add objects to parent device group

    Args:
        pan: panorama api object
        results: duplicates to be cleaned up
    Returns:
         N/A
    Raises:
        N/A
    """
    print("\nBeginning push..\n")
    # Get full objects so we can create them elsewhere
    coroutines = [
        get_objects(pan=pan, object_type=object_type, names_only=False)
        for object_type, dupes in results.items()
    ]
    print("Gathering full objects..")
    objs_list = await asyncio.gather(*coroutines)

    # Order of creation/deletion is super important...thus hard-coded here
    # Do the creates
    print("\nCreating objects...")
    await do_the_creates(
        object_types=["addresses", "services"],
        pan=pan,
        results=results,
        objs_list=objs_list,
    )
    print("\nCreating object groups...")
    await do_the_creates(
        object_types=["address-groups", "service-groups"],
        pan=pan,
        results=results,
        objs_list=objs_list,
    )

    # Now do the deletes
    print("\nDeleting object groups...")
    await do_the_deletes(
        object_types=["address-groups", "service-groups"], pan=pan, results=results
    )
    print("\nDeleting objects...")
    await do_the_deletes(
        object_types=["addresses", "services"], pan=pan, results=results
    )


async def do_the_creates(
    pan: Panorama_api, results: Dict, object_types: List[str], objs_list: Any
):
    coroutines = []
    for object_type in object_types:
        if results.get(object_type):
            for dupe, device_groups in results[object_type].items():
                dupe_obj = find_object(
                    objs_list=objs_list,
                    object_type=object_type,
                    device_groups=device_groups,
                    name=dupe,
                )

                coroutines.append(
                    pan.create_object(
                        object_type=object_type,
                        obj=dupe_obj,
                        device_group=settings.parent_device_group,
                    )
                )

    await asyncio.gather(*coroutines)


async def do_the_deletes(pan: Panorama_api, results: Dict, object_types: List[str]):
    coroutines = []
    for object_type in object_types:
        if results.get(object_type):
            for dupe, device_groups in results[object_type].items():
                for dg in device_groups:
                    coroutines.append(
                        pan.delete_object(
                            object_type=object_type, name=dupe, device_group=dg
                        )
                    )

    await asyncio.gather(*coroutines)


def find_object(objs_list, object_type, device_groups, name):
    """
    Find object to be used for creation in parent device group

    Args:
        objs_list:  List of objects to search through
        object_type:    address/group/services/groups
        device_groups:  device group
        name:   name of object to find
    Returns:
         The object you were looking for
    Raises:
        N/A
    """
    for items in objs_list:
        if items.get(object_type):
            for obj in items[object_type][
                device_groups[0]
            ]:  # Not currently checking for value, so just get object from 1st device group in list
                if obj["@name"] == name:
                    return obj
        else:
            pass


def write_output(results):
    """
    Write json string to file

    Args:
        results: dictionary of duplicate results
    Returns:
         N/A
    Raises:
        N/A
    """
    # Write output to file
    json_str = json.dumps(results, indent=4)
    with open("duplicates.json", "w") as f:
        f.write(json_str)
