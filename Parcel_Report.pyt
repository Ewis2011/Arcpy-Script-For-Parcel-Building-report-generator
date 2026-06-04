import os
import json
import arcpy


class Toolbox(object):
    def __init__(self):
        self.label = "Parcel Report Toolbox"
        self.alias = "parcel_report"
        self.tools = [GenerateParcelReport]


class GenerateParcelReport(object):
    def __init__(self):
        self.label = "Generate Parcel Report"
        self.description = (
            "Analyzes parcel and building data and writes a summary report "
            "table with building codes, total residential area, and total "
            "commercial area per parcel."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        
        parcel_layer = arcpy.Parameter(
            displayName="Parcel Layer",
            name="parcel_layer",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input"
        )
        parcel_layer.filter.list = ["Polygon"]

        building_layer = arcpy.Parameter(
            displayName="Building Layer",
            name="building_layer",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input"
        )
        building_layer.filter.list = ["Polygon"]

        output_table = arcpy.Parameter(
            displayName="Output Table",
            name="output_table",
            datatype="DETable",
            parameterType="Required",
            direction="Output"
        )

        write_mode = arcpy.Parameter(
            displayName="Write Mode (if table exists)",
            name="write_mode",
            datatype="GPString",
            parameterType="Optional",
            direction="Input"
        )
        write_mode.filter.type = "ValueList"
        write_mode.filter.list = ["Overwrite", "Append"]
        write_mode.value = "Overwrite"

        json_output = arcpy.Parameter(
            displayName="Report JSON",
            name="json_output",
            datatype="GPString",
            parameterType="Derived",
            direction="Output"
        )

        return [parcel_layer, building_layer, output_table, write_mode, json_output]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        
        parcel_lyr = parameters[0].valueAsText
        building_lyr = parameters[1].valueAsText
        out_table = parameters[2].valueAsText
        write_mode = parameters[3].valueAsText or "Overwrite"

        arcpy.AddMessage("=" * 60)
        arcpy.AddMessage("Generate Parcel Report – Execution Started")
        arcpy.AddMessage(f"  Parcels  : {parcel_lyr}")
        arcpy.AddMessage(f"  Buildings: {building_lyr}")
        arcpy.AddMessage(f"  Output   : {out_table}")
        arcpy.AddMessage(f"  Mode     : {write_mode}")
        arcpy.AddMessage("=" * 60)

        if not arcpy.Exists(parcel_lyr):
            arcpy.AddError(f"Parcel layer not found: {parcel_lyr}")
            return

        if not arcpy.Exists(building_lyr):
            arcpy.AddError(f"Building layer not found: {building_lyr}")
            return

        bld_fields = [f.name.lower() for f in arcpy.ListFields(building_lyr)]
        
        code_field = None
        for candidate in ("bld_code", "building_code", "code", "bldg_code"):
            if candidate in bld_fields:
                code_field = candidate
                break

        use_field = None
        for candidate in ("primary_use", "use", "bldg_use", "land_use"):
            if candidate in bld_fields:
                use_field = candidate
                break

        if not code_field:
            arcpy.AddError("Could not identify a building-code field (e.g., bld_code, code).")
            return
            
        if not use_field:
            arcpy.AddError("Could not identify a primary-use field (e.g., primary_use, use).")
            return

        arcpy.AddMessage(f"  Matched Building Code Field : {code_field}")
        arcpy.AddMessage(f"  Matched Primary Use Field   : {use_field}")

        prc_fields = [f.name.lower() for f in arcpy.ListFields(parcel_lyr)]
        parcel_name_field = None
        for candidate in ("parcel_name", "name", "parcel_id", "objectid", "fid"):
            if candidate in prc_fields:
                parcel_name_field = candidate
                break

        if not parcel_name_field:
            arcpy.AddWarning("No parcel name/ID field found – defaulting to system OID.")
            parcel_name_field = arcpy.Describe(parcel_lyr).OIDFieldName

        arcpy.AddMessage(f"  Matched Parcel Name Field   : {parcel_name_field}")

        if arcpy.Exists(out_table):
            if write_mode == "Overwrite":
                arcpy.AddMessage("Output table exists. Purging records (Overwrite).")
                arcpy.management.DeleteRows(out_table)
            else:
                arcpy.AddMessage("Output table exists. New records will be appended.")
        else:
            arcpy.AddMessage("Creating new output table schema...")
            out_path, out_name = os.path.split(out_table)
            if not out_path:
                out_path = arcpy.env.workspace or os.getcwd()
                
            arcpy.management.CreateTable(out_path, out_name)
            arcpy.management.AddField(out_table, "Parcel_Name", "TEXT", field_length=255)
            arcpy.management.AddField(out_table, "Inside_Buildings_Code", "TEXT", field_length=2000)
            arcpy.management.AddField(out_table, "Total_Residential_Area", "DOUBLE")
            arcpy.management.AddField(out_table, "Total_Commercial_Area", "DOUBLE")
            arcpy.AddMessage("Schema generated successfully.")

        arcpy.AddMessage("Caching building dataset into memory...")
        buildings_cache = []
        
        bld_search_fields = ["SHAPE@", code_field, use_field, "SHAPE@AREA"]
        with arcpy.da.SearchCursor(building_lyr, bld_search_fields) as cur:
            for row in cur:
                geom, bcode, buse, area = row
                if geom is None:
                    continue
                buildings_cache.append({
                    "geom": geom,
                    "code": str(bcode) if bcode is not None else "",
                    "use": str(buse).strip().lower() if buse is not None else "",
                    "area": area if area is not None else 0.0
                })

        arcpy.AddMessage(f"  Successfully cached {len(buildings_cache)} buildings.")

        report_rows = []
        insert_fields = [
            "Parcel_Name", 
            "Inside_Buildings_Code", 
            "Total_Residential_Area", 
            "Total_Commercial_Area"
        ]

        total_parcels = int(arcpy.management.GetCount(parcel_lyr)[0])
        arcpy.AddMessage(f"Processing spatial containment across {total_parcels} parcels...")

        with arcpy.da.SearchCursor(parcel_lyr, ["SHAPE@", parcel_name_field]) as p_cur, \
             arcpy.da.InsertCursor(out_table, insert_fields) as i_cur:

            for parcel_geom, parcel_name in p_cur:
                if parcel_geom is None:
                    arcpy.AddWarning(f"Parcel '{parcel_name}' contains null geometry; skipped.")
                    continue

                matched_codes = []
                res_area_sum = 0.0
                com_area_sum = 0.0

                for bld in buildings_cache:
                    try:
                        if parcel_geom.contains(bld["geom"]):
                            matched_codes.append(bld["code"])
                            if bld["use"] == "resd":
                                res_area_sum += bld["area"]
                            elif bld["use"] == "com":
                                com_area_sum += bld["area"]
                    except Exception as geom_err:
                        arcpy.AddWarning(f"Skipping bad geometry calculation on building '{bld['code']}': {geom_err}")

                codes_string = ", ".join(matched_codes) if matched_codes else ""
                rounded_res = round(res_area_sum, 4)
                rounded_com = round(com_area_sum, 4)

                i_cur.insertRow([str(parcel_name), codes_string, rounded_res, rounded_com])

                report_rows.append({
                    "Parcel_Name": str(parcel_name),
                    "Inside_Buildings_Code": matched_codes,
                    "Total_Residential_Area": rounded_res,
                    "Total_Commercial_Area": rounded_com
                })

        arcpy.AddMessage(f"Inserted {len(report_rows)} rows into target repository.")

        json_payload = json.dumps(report_rows, ensure_ascii=False, indent=4)
        parameters[4].value = json_payload
        arcpy.AddMessage("Structured data string attached successfully to 'Report JSON' pipeline.")

        arcpy.AddMessage("=" * 60)
        arcpy.AddMessage("Generate Parcel Report – Process Completed Successfully")
        arcpy.AddMessage("=" * 60)

    def postExecute(self, parameters):
        return