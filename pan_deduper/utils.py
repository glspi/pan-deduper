import asyncio
import json
import logging
from itertools import combinations

from lxml import etree
from rich.pretty import pprint

import pan_deduper.settings as settings
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


def get_objects_xml(config, object_type):
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


async def get_objects(pan, object_type, names_only=True):
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


async def run(
    *,
    configstr: str = None,
    panorama: str = None,
    username: str = None,
    password: str = None,
):
    """
    Main program

    :param configstr:
    :param panorama:
    :param username:
    :param password:
    :return:
    """
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


async def push_to_panorama(pan, results):
    coroutines = [
        get_objects(pan=pan, object_type=object_type, names_only=False)
        for object_type, dupes in results.items()
    ]
    objs_list = await asyncio.gather(*coroutines)

    for object_type, dupes in results.items():
        for dupe, device_groups in dupes.items():
            dupe_obj = find_object(
                objs_list=objs_list,
                object_type=object_type,
                device_groups=device_groups,
                name=dupe,
            )

            # Found object! (dupe_obj) now let's remove them:
            for dg in device_groups:
                await pan.delete_object(
                    object_type=object_type, name=dupe_obj["@name"], device_group=dg
                )

            # And add it to the parent group:
            print(
                await pan.create_object(
                    object_type=object_type,
                    obj=dupe_obj,
                    device_groups=settings.parent_device_group,
                )
            )


def find_object(objs_list, object_type, device_groups, name):
    for items in objs_list:
        if items.get(object_type):
            for obj in items[object_type][
                device_groups[0]
            ]:  # Not currently checking for value, so just get object from 1st device group in list
                if obj["@name"] == name:
                    return obj
        else:
            pass


def get_full_object(objs, name):
    for obj in objs:
        if obj["@name"] == name:
            return obj


def write_output(results):
    # Write output to file
    json_str = json.dumps(results, indent=4)
    with open("duplicates.json", "w") as f:
        f.write(json_str)