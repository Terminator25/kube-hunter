import json
import logging
import time
from collections import defaultdict

import requests
from prettytable import ALL, PrettyTable

from __main__ import config
from src.core.events import handler
from src.core.events.types import Service, Vulnerability

# [event, ...]
services = list()

# [(TypeClass, event), ...]
insights = list()

vulnerabilities = list()

EVIDENCE_PREVIEW = 40
MAX_WIDTH_VULNS = 70
MAX_WIDTH_SERVICES = 60

AQUA_PUSH_URL = "https://qlyscbqwl7.execute-api.us-east-1.amazonaws.com/Prod/submit?token={token}"
AQUA_RESULTS_URL = "https://qlyscbqwl7.execute-api.us-east-1.amazonaws.com/Prod/result?token={token}"

@handler.subscribe(Service)
@handler.subscribe(Vulnerability)
class Reporter(object):
    """Reportes can be initiated by the event handler, and by regular decaration. for usage on end of runtime"""
    def __init__(self, event=None):
        self.event = event
        self.insights_by_id = defaultdict(list) 
        self.services_by_id = defaultdict(list)

    def execute(self):
        """function is called only when collecting data"""
        global services, insights
        bases = self.event.__class__.__mro__
        if Service in bases:
            services.append(self.event)
            logging.info("[OPEN SERVICE - {name}] IP:{host} PORT:{port}".format(
                host=self.event.host,
                port=self.event.port,
                name=self.event.get_name(), 
                desc=self.event.explain() 
            ))
        elif Vulnerability in bases:
            insights.append((Vulnerability, self.event))
            vulnerabilities.append(self.event)
            logging.info("[VULNERABILITY - {name}] {desc}".format(
                name=self.event.get_name(),
                desc=self.event.explain(),
            ))

        if config.token:
            self.send_report(token=config.token)

    def print_tables(self):
        """generates report tables and outputs to stdout"""
        if len(services):
            print_nodes()
            if not config.mapping:
                print_services()
                print_vulnerabilities()
        else:
            print "\nKube Hunter couldn't find any clusters"
            # print "\nKube Hunter couldn't find any clusters. {}".format("Maybe try with --active?" if not config.active else "")

    def build_sub_services(self, services_list):
        # correlation functions
        def get_insights_by_service(service):
            """generates list of insights related to a given service"""
            insights = list()
            for insight_type, insight in self.insights_by_id[service.event_id]:
                if service in insight.history:
                    insights.append((insight_type, insight))
            return insights
            
        def get_services_by_service(parent_service):
            """generates list of insights related to a given service"""
            services = list()
            for service in self.services_by_id[parent_service.event_id]:
                if service != parent_service and parent_service in service.history:
                    services.append(service)
                    self.services_by_id[parent_service.event_id].remove(service)
            return services

        current_list = list()
        for service in services_list:
            current_list.append(
            {
                "type": service.get_name(),
                "metadata": {
                    "port": service.port,
                    "path": service.get_path()
                },
                "description": service.explain()
            })
            next_services = get_services_by_service(service)
            if next_services:
                current_list[-1]["services"] = self.build_sub_services(next_services)
            current_list[-1]["insights"] = [{
                "type": insight_type.__name__,
                "name": insight.get_name(),
                "description": insight.explain(),
                "evidence": insight.evidence if insight_type == Vulnerability else ""
            } for insight_type, insight in get_insights_by_service(service)]
        return current_list

    def send_report(self, token):
        def generate_report():
            """function generates a report corresponding to specifications of the frontend of kubehunter"""
            for service in services:
                self.services_by_id[service.event_id].append(service)
            for insight_type, insight in insights:
                self.insights_by_id[insight.event_id].append((insight_type, insight))

            # building first layer of services (nodes)
            report = defaultdict(list)
            for _, services_list in self.services_by_id.items():
                service_report = {
                    "type": "Node", # on future, determine if slave or master
                    "metadata": {
                        "host": str(services_list[0].host)
                    },
                    # then constructing their sub services tree
                    "services": self.build_sub_services(services_list)
                } 
                report["services"].append(service_report)
            return report
        
        finished = (not handler.unfinished_tasks)
        logging.debug("generating report")
        report = {
            'results': generate_report(),
            'metadata': {
                'finished': finished
            } 
        } 
        logging.debug("uploading report")
        r = requests.put(AQUA_PUSH_URL.format(token=token), json=report)
        
        if r.status_code == 201: # created status
            logging.debug("report was uploaded successfully") 
            if finished:       
                print "\nYour report: \n{}".format(AQUA_RESULTS_URL.format(token=token))
        else:
            logging.debug("Failed sending report with:{}, {}".format(r.status_code, r.text))
            if finished:
                print "\nCould not send report.\n{}".format(json.loads(r.text).get("status", ""))

reporter = Reporter()


""" Tables Generation """
def print_nodes():
    nodes_table = PrettyTable(["Type", "Location"], hrules=ALL)
    nodes_table.align="l"     
    nodes_table.max_width=MAX_WIDTH_SERVICES  
    nodes_table.padding_width=1
    nodes_table.sortby="Type"
    nodes_table.reversesort=True  
    nodes_table.header_style="upper"
    
    # TODO: replace with sets
    id_memory = list()
    for service in services:
        if service.event_id not in id_memory:
            nodes_table.add_row(["Node/Master", service.host])
            id_memory.append(service.event_id)
    print "Nodes:"
    print nodes_table
    print 

def print_services():
    services_table = PrettyTable(["Service", "Location", "Description"], hrules=ALL)
    services_table.align="l"     
    services_table.max_width=MAX_WIDTH_SERVICES  
    services_table.padding_width=1
    services_table.sortby="Service"
    services_table.reversesort=True  
    services_table.header_style="upper"
    for service in services:
        services_table.add_row([service.get_name(), "{}:{}{}".format(service.host, service.port, service.get_path()), service.explain()])
    print "Detected Services:"
    print services_table
    print 

def print_vulnerabilities():
    column_names = ["Location", "Category", "Vulnerability", "Description"]
    if config.active: column_names.append("Evidence")
    vuln_table = PrettyTable(column_names, hrules=ALL)
    vuln_table.align="l"
    vuln_table.max_width=MAX_WIDTH_VULNS 
    vuln_table.sortby="Category"    
    vuln_table.reversesort=True
    vuln_table.padding_width=1
    vuln_table.header_style="upper"    
    for vuln in vulnerabilities:
        row = ["{}:{}".format(vuln.host, vuln.port) if vuln.host else "", vuln.component.name, vuln.get_name(), vuln.explain()]
        if config.active: 
            evidence = str(vuln.evidence)[:EVIDENCE_PREVIEW] + "..." if len(str(vuln.evidence)) > EVIDENCE_PREVIEW else str(vuln.evidence)
            row.append(evidence)
        vuln_table.add_row(row)        
    print "Vulnerabilities:"
    print vuln_table
    print 