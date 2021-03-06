#!/usr/bin/env python
# from setuptools import setup, find_packages
import os
import tarfile
import yaml
import urllib2
import psycopg2
import cookielib
import re
import json
import cassandra
from cassandra.cluster import Cluster
from cassandra.concurrent import execute_concurrent
import urllib
import datetime
import classification
import matplotlib.pyplot as plt
import matplotlib.cbook as cbook
import math
import sys
import agglomerative
import clustering
import blob_clustering
from collections import OrderedDict
import numpy
import random
import cPickle as pickle
# setup(
#     name = "Zooniverse Aggregation",
#     version = "0.1",
#     packages = find_packages(),
# )

if os.path.exists("/home/ggdhines"):
    base_directory = "/home/ggdhines"
else:
    base_directory = "/home/greg"

class InvalidMarking(Exception):
    def __init__(self,pt):
        self.pt = pt
    def __str__(self):
        return "invalid marking: " + str(self.pt)


class ImageNotDownloaded(Exception):
    def __init__(self):
        pass
    def __str__(self):
        return 'image not downloaded'


class ImproperTool(Exception):
    def __init__(self,tool):
        self.tool = tool
    def __str__(self):
        return "improper tool: " + str(self.tool)


# extract the relevant params for different shapes from the json blob
# todo - do a better job of checking to make sure that the marking lies within the image dimension
# todo - also generalize to ROI
def relevant_line_params(marking,image_dimensions):
    # want to extract the params x1,x2,y1,y2 but
    # ALSO make sure that x1 <= x2 and flip if necessary
    x1 = marking["x1"]
    x2 = marking["x2"]
    y1 = marking["y1"]
    y2 = marking["y2"]

    if min(x1,x2,y1,y2) < 0:
        raise InvalidMarking(marking)

    if (max(x1,x2) >= image_dimensions[0]) or (max(y1,y2) >= image_dimensions[1]):
        raise InvalidMarking(marking)

    if x1 <= x2:
        return x1,y1,x2,y2
    else:
        return x2,y2,x1,y1


def relevant_point_params(marking,image_dimensions):
    # todo - this has to be changed
    image_dimensions = 1000,1000
    if (marking["x"] == "") or (marking["y"] == ""):
        raise InvalidMarking(marking)

    try:
        x = float(marking["x"])
        y = float(marking["y"])
    except ValueError:
        print marking
        raise

    if (x<0)or(y<0)or(x > image_dimensions[0]) or(y>image_dimensions[1]):
        raise InvalidMarking(marking)

    return x,y


def relevant_rectangle_params(marking,image_dimensions):
    x = marking["x"]
    y = marking["y"]

    x2 = x + marking["width"]
    y2 = y + marking["height"]

    if (x<0)or(y<0)or(x2 > image_dimensions[0]) or(y2>image_dimensions[1]):
        raise InvalidMarking(marking)

    # return x,y,x2,y2
    return (x,y),(x,y2),(x2,y2),(x2,y)


def relevant_circle_params(marking,image_dimensions):
    return marking["x"],marking["y"],marking["r"]


def relevant_ellipse_params(marking,image_dimensions):
    return marking["x"],marking["y"],marking["rx"],marking["ry"],marking["angle"]


# mappings are for use in dimension reduction
def identity_mapping(markings):
    return markings


def hesse_line_reduction(line_segments):
    """
    use if we want to cluster based on Hesse normal form - but want to retain the original values
    :param line_segment:
    :return:
    """
    reduced_markings = []

    for line_seg in line_segments:
        x1,y1,x2,y2 = line_seg

        x2 += random.uniform(-0.0001,0.0001)
        x1 += random.uniform(-0.0001,0.0001)

        dist = (x2*y1-y2*x1)/math.sqrt((y2-y1)**2+(x2-x1)**2)

        try:
            tan_theta = math.fabs(y1-y2)/math.fabs(x1-x2)
            theta = math.atan(tan_theta)
        except ZeroDivisionError:
            theta = math.pi/2.

        reduced_markings.append((dist,theta))

    return reduced_markings




class AggregationAPI:
    def __init__(self,project=None,environment=None,db_connection=True,user_id=None,password=None,cassandra_connection=True):#,user_threshold= None, score_threshold= None): #Supernovae

        self.cluster_algs = None
        self.classification_alg = None
        self.workflows = None
        self.versions = None
        self.classifications = None

        # functions for converting json instances into values we can actually cluster on
        self.marking_params_per_shape = dict()
        self.marking_params_per_shape["line"] = relevant_line_params
        self.marking_params_per_shape["point"] = relevant_point_params
        self.marking_params_per_shape["ellipse"] = relevant_ellipse_params
        self.marking_params_per_shape["rectangle"] = relevant_rectangle_params
        self.marking_params_per_shape["circle"] = relevant_circle_params

        # in case project wants to have an roi
        self.roi_dict = {}

        # default value
        if environment is None:
            self.environment = "production"
        else:
            self.environment = environment

        self.host_api = None
        self.subject_id_type = "int"

        self.experts = []

        # only continue the set up if the project name is given
        # self.project_short_name = project

        # connect to the Panoptes db - postgres
        # even if we are running an ouroboros project we will want to connect to Postgres
        if db_connection:
            try:
                database_file = open("config/database.yml")
            except IOError:
                database_file = open(base_directory+"/Databases/database.yml")

            database_details = yaml.load(database_file)
            self.postgres_session = None
            self.__postgres_connect__(database_details[self.environment])

        if cassandra_connection:
            # and to the cassandra db as well
            self.__cassandra_connect__(environment)

            # will only override this for ouroboros instances
            self.classification_table = "classifications"

        # get my userID and password
        # purely for testing, if this file does not exist, try opening on Greg's computer
        try:
            panoptes_file = open("config/aggregation.yml","rb")
        except IOError:
            panoptes_file = open(base_directory+"/Databases/aggregation.yml","rb")
        api_details = yaml.load(panoptes_file)

        print "connecting to Panoptes http api"
        if user_id is None:
            user_id = api_details[environment]["name"]
        if password is None:
            password = api_details[environment]["password"]
        # set the http_api and basic project details
        # if project id is given, connect using basic values - assume we are in production space
        # if project id is given as an int, assume that it is referring to the Panoptes id
        self.__panoptes_connect__(api_details[environment],user_id,password)

        if project is None:
            return

        try:
            self.project_id = int(project)
        except ValueError:
            # we were given a project string name - so try looking for the number in either config file
            # or connect to panoptes to fine out
            if "project_id" in api_details[project]:
                self.project_id = int (api_details[project]["project_id"])
            else:
                # owner and project_id are only relevant if we do not have the project_id
                self.owner = api_details[project]["owner"]
                self.project_name = api_details[project]["project_name"]
                self.project_id = self.__get_project_id()

        # there may be more than one workflow associated with a project - read them all in
        # and set up the associated tasks
        self.workflows,self.versions,self.instructions,self.updated_at_timestamps = self.__get_workflow_details__()

        # load the default clustering algorithms
        default_clustering_algs = {"point":agglomerative.Agglomerative,"circle":agglomerative.Agglomerative,"ellipse": agglomerative.Agglomerative,"rectangle":blob_clustering.BlobClustering,"line":agglomerative.Agglomerative}
        reduction_algs = {"line":hesse_line_reduction}
        self.__set_clustering_algs__(default_clustering_algs,reduction_algs)

        # load the default classification algorithm
        self.__set_classification_alg__(classification.VoteCount)

        # todo - do in this in Cassandra
        # for reading in from classifications only done since the last run
        # if any trouble - start over from the beginning
        try:
            self.old_time = pickle.load(open("/tmp/"+str(self.project_id)+".time","rb"))
        except:
            self.old_time = datetime.datetime(2000,01,01)
        # self.old_time = datetime.datetime(2000,01,01)

        self.current_time = datetime.datetime.now()
        self.ignore_versions = False

    def __aggregate__(self,workflows=None,subject_set=None,gold_standard_clusters=([],[]),expert=None,store_values=True):
        """
        you can provide a list of clusters - hopefully examples of both true positives and false positives
        note this means you have already run the aggregation before and are just coming back with
        more info
        for now, only one expert - easily generalizable but just want to avoid the situation where
        multiple experts have marked the same subject - need to be a bit careful there
        :param workflows:
        :param subject_set:
        :param gold_standard_clusters:
        :return:
        """

        # todo - set things up so that you don't have to redo all of the aggregations just to rerun ibcc
        if workflows is None:
            workflows = self.workflows

        given_subject_set = (subject_set != None)

        for workflow_id in workflows:
            if subject_set is None:
                subject_set = self.__get_retired_subjects__(workflow_id)
                # subject_set = self.__load_subjects__(workflow_id)
            print "workflow id : " + str(workflow_id)
            print "aggregating " + str(len(subject_set)) + " subjects"
            # self.__describe__(workflow_id)
            classification_tasks,marking_tasks = self.workflows[workflow_id]

            clustering_aggregations = None
            classification_aggregations = None

            # if we have provided a clustering algorithm and there are marking tasks
            # ideally if there are no marking tasks, then we shouldn't have provided a clustering algorithm
            # but nice sanity check
            # print sorted(subject_set)
            raw_classifications,raw_markings = self.__sort_annotations__(workflow_id,subject_set,expert)

            # do we have any marking tasks?
            if  marking_tasks != {}:
                print "clustering"
                clustering_aggregations = self.__cluster__(raw_markings)
                # assert (clustering_aggregations != {}) and (clustering_aggregations is not None)
            if (self.classification_alg is not None) and (classification_tasks != {}):
                # we may need the clustering results
                print "classifying"
                classification_aggregations = self.__classify__(raw_classifications,clustering_aggregations,workflow_id,gold_standard_clusters)

            print "***"
            print self.classification_alg
            print marking_tasks
            print classification_tasks

            # if we have both markings and classifications - we need to merge the results
            if (clustering_aggregations is not None) and (classification_aggregations is not None):
                aggregations = self.__merge_aggregations__(clustering_aggregations,classification_aggregations)
            elif clustering_aggregations is None:
                aggregations = classification_aggregations
            else:
                aggregations = clustering_aggregations

            # unless we are provided with specific subjects, reset for the extra workflow
            if not given_subject_set:
                subject_set = None

            # finally, store the results
            # if gold_standard_clusters is not None, assume that we are playing around with values
            # and we don't want to automatically save the results

            # print aggregations["APZ0002do1"]
            # print clustering_aggregations["APZ0002do1"]
            if store_values:
                self.__upsert_results__(workflow_id,aggregations)
            else:
                return aggregations

    def __cassandra_connect__(self, env):
        """
        connect to the AWS instance of Cassandra - try 10 times and raise an error
        :return:
        """
        for i in range(10):
            try:
                if env == 'production':
                    self.cluster = Cluster(['panoptes-cassandra.zooniverse.org'])
                if env == 'staging':
                    self.cluster = Cluster(['panoptes-cassandra-staging.zooniverse.org'])
                else:
                    self.cluster = Cluster(['cassandra'])

                # self.cluster = Cluster()

                try:
                    self.cassandra_session = self.cluster.connect("zooniverse")
                except cassandra.InvalidRequest:
                    cassandra_session = self.cluster.connect()
                    cassandra_session.execute("CREATE KEYSPACE zooniverse WITH REPLICATION = { 'class' : 'SimpleStrategy', 'replication_factor' : 2 }")
                    self.cassandra_session = self.cluster.connect('zooniverse')

                return
            except cassandra.cluster.NoHostAvailable:
                pass

        assert False

    def __chunks(self,l, n):
        """Yield successive n-sized chunks from l."""
        for i in xrange(0, len(l), n):
            yield l[i:i+n]

    def __classify__(self,raw_classifications,clustering_aggregations,workflow_id,gold_standard_classifications=None):
        # get the raw classifications for the given workflow
        # raw_classifications = self.__sort_classifications__(workflow_id,subject_set)
        if raw_classifications == {}:
            print "returning empty"
            empty_aggregation = {"param":"subject_id"}
            # for subject_set in empty_aggregation
            return empty_aggregation
        # assert False
        return self.classification_alg.__aggregate__(raw_classifications,self.workflows[workflow_id],clustering_aggregations,gold_standard_classifications)

    def __cluster__(self,raw_markings):
        """
        run the clustering algorithm for a given workflow
        need to have already checked that the workflow requires clustering
        :param workflow_id:
        :return:
        """
        # assert (self.cluster_algs != {}) and (self.cluster_algs is not None)
        # print "workflow id is " + str(workflow_id)
        # get the raw classifications for the given workflow
        # if subject_set is None:
        #     subject_set = self.__load_subjects__(workflow_id)

        # raw_markings = self.__sort_markings__(workflow_id,subject_set)

        if raw_markings == {}:
            print "warning - empty set of images"
            # print subject_set
            return {}
        # assert raw_markings != {}
        # assert False

        # will store the aggregations for all clustering
        cluster_aggregation = {}
        for shape in self.cluster_algs:
            shape_aggregation = self.cluster_algs[shape].__aggregate__(raw_markings)

            if cluster_aggregation == {}:
                cluster_aggregation = shape_aggregation
            else:
                assert isinstance(cluster_aggregation,dict)
                cluster_aggregation = self.__merge_aggregations__(cluster_aggregation,shape_aggregation)

        return cluster_aggregation

    def __csv_output__(self,given_workflows = None, compress = False):
        if given_workflows is None:
            workflows = self.workflows
        else:
            workflows = [given_workflows]

        print self.workflows

        self.clustering_headers = ["subject_id", "shape","cluster_index","most_likely_tool", "x", "y", "height","width","rotation", "number_of_users_per_subject", "number_of_users_per_cluster","probability_of_existence", "probability_of_most_likely_tool"]
        self.classification_headers = ["subject_id",  "number_of_users"]
        # todo - the following are only relevant for followup questions which still need to be done
        # "shape","cluster_index","followup_index",

        headers_per_task = {}

        if compress:
            tarball = tarfile.open("/tmp/"+str(self.project_id)+"export.tar.gz", "w:gz")

        for workflow_id in workflows:
            json.dump(self.instructions[workflow_id],open("/tmp/workflow_"+str(workflow_id)+"_instructions.json","wb"),indent=4)

            # yield task_id,r[0],int(cluster_index),shape,int(most_likely_tool),p_x,p_y,height,width,rotation,number_of_users_per_subject,number_of_users_per_cluster,probability_of_existence,tool_probability
            classification_tasks,marking_tasks = self.workflows[workflow_id]
            csv_files = {}
            for c in classification_tasks:
                if isinstance(classification_tasks[c],dict):
                    print "skipping"
                    continue

                csv_files[c+"c"] = open("/tmp/workflow_"+str(workflow_id)+"_task_"+c+"_classification.csv","wb")
                # csv_files[c+"c"].write("subject id, shape,cluster index,followup index, most likely label, probability of most likely label, number of users\n")
                # start with the basic headers and add on ones specific to the task at hand
                headers_per_task[c] = self.classification_headers[:]
                for answers_index in self.instructions[workflow_id][c]["answers"]:
                    answer = str(self.instructions[workflow_id][c]["answers"][answers_index])
                    # bit of a sanity check
                    answer = answer.replace("\"","")
                    answer = answer.replace(",","")
                    headers_per_task[c].append("p\""+answer+"\"")

                header_string = ",".join(headers_per_task[c]) + "\n"
                # print classification_tasks
                # print c
                # print self.instructions[workflow_id][c]
                # for answers_index in self.instructions[workflow_id][c]["answers"]:
                #     headers += "p("+str(self.instructions[workflow_id][c]["answers"][answers_index])+"),"

                # headers = headers[:-1] + "\n"
                csv_files[c+"c"].write(header_string)

            for c in marking_tasks:
                csv_files[c+"m"] = open("/tmp/workflow_"+str(workflow_id)+"_task_"+c+"_marking.csv","wb")
                headers = ",".join(self.clustering_headers) + "\n"
                csv_files[c+"m"].write(headers)

            for aggregation_dict in self.__get_aggregations__(workflow_id):
                # subject_id = record[1]
                # print aggregation_dict
                # subject_id = aggregation_dict["subject_id"]
                if aggregation_dict["t"] == "classification":
                    f = csv_files[aggregation_dict["task_id"]+"c"]
                    assert isinstance(f,file)

                    csv_line = ""
                    for field in headers_per_task[aggregation_dict["task_id"]]:
                        csv_line += str(aggregation_dict[field]) + ","
                    f.write(csv_line[:-1]+"\n")

                    # probabilities = record[2][0].items()
                    # most_likely, highest_prob = max(probabilities, key = lambda x:x[1])
                    # num_users = record[2][1]
                    # f.write(str(subject_id) + ",,,," + str(most_likely) + "," + str(highest_prob) + "," + str(num_users) +"\n")
                else:
                    assert aggregation_dict["t"] == "marking"
                    # marking
                    # print aggregation_dict.keys()
                    if aggregation_dict["probability_of_existence"] < 0.5:
                        continue

                    f = csv_files[aggregation_dict["task_id"]+"m"]
                    csv_line = ""
                    for field in self.clustering_headers:
                        if field in aggregation_dict:
                            csv_line += str(aggregation_dict[field]) + ","
                        else:
                            csv_line += ","

                    f.write(csv_line[:-1]+"\n")

                    # there were followup questions
                    if "followup questions" in aggregation_dict:
                        for followup_index,followup_question in enumerate(record[-1]):
                            num_users = followup_question[1]
                            if num_users != 0:
                                probabilities = followup_question[0].items()
                                most_likely, highest_prob = max(probabilities, key = lambda x:x[1])

                                f = csv_files[record[0]+"c"]
                                shape = record[2]
                                cluster_index = record[3]
                                f.write(str(subject_id)+","+str(shape)+","+str(cluster_index)+","+str(followup_index)+","+str(most_likely)+","+str(highest_prob)+","+str(num_users))


            for f in csv_files.values():
                assert isinstance(f,file)
                if compress:
                    tarInfo = tarball.gettarinfo(fileobj=f)
                    tarball.addfile(tarInfo, fileobj=f)
                f.close()

        if compress:
            tarball.close()
            return "/tmp/"+str(self.project_id)+"export.tar.gz"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pickle.dump(self.current_time,open("/tmp/"+str(self.project_id)+".time","wb"))
        self.cassandra_session.shutdown()

    # def __describe__(self,workflow_id):
    #     select = "SELECT tasks from workflows where id = " + str(workflow_id)
    #     self.postgres_cursor.execute(select)
    #     tasks = self.postgres_cursor.fetchone()[0]
    #
    #     select = "SELECT strings from workflow_contents where id = " + str(workflow_id)
    #     self.postgres_cursor.execute(select)
    #     contents = self.postgres_cursor.fetchone()[0]
    #
    #     self.description = {}
    #     print "===---"
    #     for task_id in tasks:
    #         print tasks[task_id].keys()
    #         # print task_id
    #         self.description[task_id] = []
    #         # print tasks[task_id]
    #         if "question" in tasks[task_id]:
    #             question = tasks[task_id]["question"]
    #             self.description[task_id].append(contents[question])
    #             # print contents[question]
    #             answers = tasks[task_id]["answers"]
    #             # print answers
    #             for ans in answers:
    #                 # print ans
    #                 label = ans["label"]
    #                 labels = label.split(".")
    #                 # question_index = labels[2]
    #                 self.description[task_id].append(contents[label])
    #                 # print self.description[task_id][-1]
    #         else:
    #             assert "tools" in tasks[task_id]
    #             print tasks[task_id]["tools"]
    #     # self. description

    def __get_aggregations__(self,workflow_id):
        # if workflows is None:
        #     workflows = self.workflows
        # elif isinstance(workflows,int):
        #     # just in case we didn't provide the workflows as a list, be nice and convert
        #     workflows = [workflows]

        stmt = "select subject_id,aggregation,updated_at from aggregations where workflow_id = " + str(workflow_id)
        cursor = self.postgres_session.cursor()

        cursor.execute(stmt)

        for r in cursor.fetchall():
            aggregation = r[1]

            if isinstance(aggregation,str):
                aggregation = json.loads(aggregation)
            elif not isinstance(aggregation,dict):
                print type(aggregation)
            assert isinstance(aggregation,dict)


            for task_id in aggregation:
                if task_id in [" instructions"," metadata","param"]:
                    continue

                # we have an instance of marking
                if isinstance(aggregation[task_id],dict):
                    for cluster_type in aggregation[task_id]:
                        if cluster_type != "param":
                            for cluster_index in aggregation[task_id][cluster_type]:
                                if cluster_index not in ["param","all_users"]:
                                    cluster = aggregation[task_id][cluster_type][cluster_index]

                                    # build up the dictionary to return
                                    # omit any values that aren't relevant
                                    aggregation_dict = {"subject_id":r[0],"t":"marking","cluster_index":cluster_index,"task_id":task_id}

                                    aggregation_dict["shape"] = cluster_type.split(" ")[0]

                                    if cluster_type == "point clusters":
                                        aggregation_dict["x"] = cluster["center"][0]
                                        aggregation_dict["y"] = cluster["center"][1]
                                        height = None
                                        width = None
                                        rotation = None
                                    elif cluster_type == "rectangle clusters":
                                        p_x,_py = cluster["center"][0]
                                        p_x2,p_y2 = cluster["center"][2]
                                        width = math.fabs(p_x2 - p_x)
                                        height = math.fabs(p_y2 - p_y2)
                                        rotation = None
                                    elif cluster_type == "circle clusters":
                                        # p_x = cluster["center"][0]
                                        aggregation_dict["x"] = cluster["center"][0]
                                        # p_y = cluster["center"][1]
                                        aggregation_dict["y"] = cluster["center"][1]
                                        height = cluster["center"][2]
                                        width = None
                                        rotation = None
                                    elif cluster_type == "ellipse clusters":
                                        p_x = cluster["center"][0]
                                        p_y = cluster["center"][1]
                                        height = cluster["center"][2]
                                        width = cluster["center"][3]
                                        rotation = cluster["center"][4]
                                    elif cluster_type == "line clusters":
                                        aggregation_dict["x"] = cluster["center"][0]
                                        aggregation_dict["y"] = cluster["center"][1]
                                        aggregation_dict["height"] = cluster["center"][1] - cluster["center"][3]
                                        aggregation_dict["width"] = cluster["center"][0] - cluster["center"][2]
                                        # p_x = cluster["center"][0]
                                        # p_y = cluster["center"][1]
                                        # height = cluster["center"][1] - cluster["center"][3]
                                        # width = cluster["center"][0] - cluster["center"][2]
                                        # rotation = None
                                    else:
                                        print cluster_type, cluster
                                        assert False
                                    # "number_of_users_per_subject", "number_of_users_per_cluster","probability_of_existence"

                                    print cluster

                                    aggregation_dict["number_of_users_per_subject"] = cluster["existence"][1]
                                    aggregation_dict["probability_of_existence"] = cluster["existence"][0]["1"]
                                    # number_of_users_per_subject = cluster["existence"][1]
                                    # probability_of_existence = cluster["existence"][0]["1"]

                                    aggregation_dict["number_of_users_per_cluster"] = cluster["num users"]

                                    # only happens when different tools produce the same shape
                                    if "tool_classification" in cluster:
                                        tool_classification = cluster["tool_classification"][0]

                                        most_likely_tool_index,tool_probability = max(tool_classification.items(), key = lambda x:x[1])

                                        most_likely_tool = self.instructions[workflow_id][task_id]["tools"][int(most_likely_tool_index)]["marking tool"]

                                        aggregation_dict["most_likely_tool"] = most_likely_tool
                                        aggregation_dict["probability_of_most_likely_tool"] = tool_probability

                                    else:
                                        aggregation_dict["most_likely_tool"] = self.instructions[workflow_id][task_id]["tools"][0]["marking tool"]
                                        tool_probability = None

                                    if "followup_questions" in cluster:
                                        followup_questions = cluster["followup_questions"]
                                    else:
                                        followup_questions = None

                                    yield aggregation_dict
                                    # yield task_id,r[0],shape,int(cluster_index),int(most_likely_tool),p_x,p_y,height,width,rotation,number_of_users_per_subject,number_of_users_per_cluster,probability_of_existence,tool_probability,followup_questions
                else:
                    classification = aggregation[task_id][0]
                    # max_class_index = max([int(c) for c in classification_record])
                    # for class_index in range(max_class_index+1):
                    #     if str(class_index) not in classification_record:
                    #         print 0
                    #     else:
                    #         print classification_record[str(class_index)]
                    # yield task_id,r[0],aggregation[task_id]
                    aggregation_dict = {"subject_id":r[0],"t":"classification","task_id":task_id,"number_of_users":aggregation[task_id][1]}

                    for answer_index,answer in self.instructions[workflow_id][task_id]["answers"].items():
                        answer = answer.replace("\"","")
                        answer = answer.replace(",","")
                        kw = "p\""+str(answer)+"\""
                        if str(answer_index) in classification:
                            aggregation_dict[kw] = classification[str(answer_index)]
                        else:
                            aggregation_dict[kw] = 0

                    yield aggregation_dict


    def __get_aggregated_subjects__(self,workflow_id):
        """
        return a list of subjects which have aggregation results
        todo: - make sure it for panoptes - not just penguins
        :param workflow_id:
        :return:
        """
        stmt = "select subject_id from aggregations where workflow_id = " + str(workflow_id)
        self.postgres_cursor.execute(stmt)

        subjects = []

        for r in self.postgres_cursor.fetchall():
            subjects.append(r[0])

        return subjects

    def __get_num_clusters__(self,subject_id,task_id):
        return len(self.cluster_alg.clusterResults[subject_id][task_id])

    def __get_classifications__(self,subject_id,task_id,cluster_index=None,question_id=None):
        # either both of these variables are None or neither of them are
        assert (cluster_index is None) == (question_id is None)

        if cluster_index is None:
            return self.classifications[subject_id][task_id]
        else:
            return self.classifications[subject_id][task_id][cluster_index][question_id]

    def __get_raw_classifications__(self,subject_id,workflow_id):
        version = int(math.floor(float(self.versions[workflow_id])))
        select_statement = self.cassandra_session.prepare("select annotations from "+self.classification_table+" where project_id = ? and subject_id = ? and workflow_id = ? and workflow_version = ?")
        params = (int(self.project_id),subject_id,int(workflow_id),version)
        statements_and_params = []
        statements_and_params.append((select_statement, params))
        results = execute_concurrent(self.cassandra_session, statements_and_params, raise_on_first_error=False)

        assert results[0][0]
        for r in results[0][1]:
            yield json.loads(r.annotations)

        raise StopIteration()



    def __get_subject_dimension__(self,subject_id):
        """
        get the x and y size of the subject
        useful for plotting and also for checking if we have any bad points
        if no dimension is founds, return None,None
        :param subject_id:
        :return:
        """
        return None

    def __get_project_id(self):
        """
        get the id number for our project
        this is mostly a "beta" feature so that given a name of a project, I can find the id
        in practice, aggregation will be run with just giving the project id
        :return:
        """
        # print self.host_api+"projects?owner="+urllib2.quote(self.owner)+"&display_name="+urllib2.quote(self.project_name)
        # assert False
        # request = urllib2.Request(self.host_api+"projects?owner="+urllib2.quote(self.owner)+"&display_name="+urllib2.quote(self.project_name))
        # print self.host_api+"projects?display_name="+urllib2.quote(self.project_name)
        request = urllib2.Request(self.host_api+"projects?display_name="+urllib2.quote(self.project_name))
        # request = urllib2.Request(self.host_api+"projects?owner="+self.owner+"&display_name=Galaxy%20Zoo%20Bar%20Lengths")
        # print hostapi+"projects?owner="+owner+"&display_name="+project_name
        request.add_header("Accept","application/vnd.api+json; version=1")
        request.add_header("Authorization","Bearer "+self.token)

        # request
        try:
            response = urllib2.urlopen(request)
            body = response.read()
        except urllib2.HTTPError as e:
            print self.host_api+"projects?owner="+self.owner+"&display_name="+self.project_name
            print 'The server couldn\'t fulfill the request.'
            print 'Error code: ', e.code
            print 'Error response body: ', e.read()
            raise
        except urllib2.URLError as e:
            print 'We failed to reach a server.'
            print 'Reason: ', e.reason
            raise

        data = json.loads(body)
        # put it in json structure and extract id
        return data["projects"][0]["id"]
        # return None

    def __get_retired_subjects__(self,workflow_id,with_expert_classifications=None):
        retired_subjects = []

        stmt = """SELECT * FROM "subjects"
            INNER JOIN "set_member_subjects" ON "set_member_subjects"."subject_id" = "subjects"."id"
            INNER JOIN "subject_workflow_counts" ON "subject_workflow_counts"."set_member_subject_id" = "set_member_subjects"."id"
            WHERE "subject_workflow_counts"."workflow_id" = """+str(workflow_id)+ """ AND "subject_workflow_counts"."retired_at" > '""" + str(self.updated_at_timestamps[workflow_id]) + """'"""
            # WHERE "subject_workflow_counts"."workflow_id" = """+str(workflow_id)+ """ AND "subject_workflow_counts"."retired_at" IS NOT NULL"""

        cursor = self.postgres_session.cursor()
        cursor.execute(stmt)
        for subject in cursor.fetchall():
            retired_subjects.append(subject[0])

        return retired_subjects

    def __get_subjects__(self,workflow_id,only_retired_subjects=False,only_recent_subjects=True):
        subjects = []
        if only_retired_subjects:
            if only_recent_subjects:
                timestamp = self.updated_at_timestamps[workflow_id]
            else:
                timestamp = datetime.datetime(2000,01,01)

            stmt = """SELECT * FROM "subjects"
            INNER JOIN "set_member_subjects" ON "set_member_subjects"."subject_id" = "subjects"."id"
            INNER JOIN "subject_workflow_counts" ON "subject_workflow_counts"."set_member_subject_id" = "set_member_subjects"."id"
            WHERE "subject_workflow_counts"."workflow_id" = """+str(workflow_id)+ """ AND "subject_workflow_counts"."retired_at" > '""" + str(datetime.datetime(2000,01,01)) + """'"""
            # WHERE "subject_workflow_counts"."workflow_id" = """+str(workflow_id)+ """ AND "subject_workflow_counts"."retired_at" IS NOT NULL"""

            cursor = self.postgres_session.cursor()
            cursor.execute(stmt)
            for subject in cursor.fetchall():
                subjects.append(subject[0])
        else:
            stmt = "SELECT subject_id FROM subjects WHERE project_id = " + str(self.project_id) + " and workflow_id = " + str(workflow_id)# + " and workflow_version = " + str(version)
            subjects = set([r.subject_id for r in self.cassandra_session.execute(stmt)])

        return subjects


    def __get_workflow_details__(self,given_workflow_id=None):
        """
        get everything about the workflows - if no id is provided, go with everything
        :param workflow_id:
        :return:
        """
        request = urllib2.Request(self.host_api+"workflows?project_id="+str(self.project_id))
        # request = urllib2.Request(self.host_api+"workflows/project_id="+str(self.project_id))
        request.add_header("Accept","application/vnd.api+json; version=1")
        request.add_header("Authorization","Bearer "+self.token)

        # request
        try:
            response = urllib2.urlopen(request)
        except urllib2.HTTPError as e:
            sys.stderr.write('The server couldn\'t fulfill the request.\n')
            sys.stderr.write('Error code: ' + str(e.code) + "\n")
            sys.stderr.write('Error response body: ' + str(e.read()) + "\n")
            raise
        except urllib2.URLError as e:
            sys.stderr.write('We failed to reach a server.\n')
            sys.stderr.write('Reason: ' + str(e.reason) + "\n")
            raise
        else:
            # everything is fine
            body = response.read()

        # put it in json structure and extract id
        data = json.loads(body)

        instructions = {}
        workflows = {}
        updated_at_timestamps = {}
        versions = {}

        print "+++"
        print "+++"

        for individual_workflow in data["workflows"]:
            workflow_id = int(individual_workflow["id"])
            if (given_workflow_id is None) or (workflow_id == given_workflow_id):
                # read in the basic structure of the workflow
                workflows[workflow_id] = self.__readin_tasks__(workflow_id)
                print workflows[workflow_id]

                # read in the instructions associated with the workflow
                # not used for the actual aggregation but for printing out results to the user
                instructions[workflow_id] = {}
                for task_id,task in individual_workflow["tasks"].items():
                    instructions[workflow_id][task_id] = {}
                    # classification task
                    if "question" in task:
                        question = task["question"]
                        instructions[workflow_id][task_id]["instruction"] = re.sub("'","",question)
                        instructions[workflow_id][task_id]["answers"] = {}
                        for answer_id,answer in enumerate(task["answers"]):
                            label = answer["label"]
                            label = re.sub("'","",label)
                            instructions[workflow_id][task_id]["answers"][answer_id] = label

                    else:
                        instruct_string = task["instruction"]
                        instructions[workflow_id][task_id]["instruction"] = re.sub("'","",instruct_string)

                        instructions[workflow_id][task_id]["tools"] = {}
                        for tool_index,tool in enumerate(task["tools"]):
                            instructions[workflow_id][task_id]["tools"][tool_index] = {}
                            label = tool["label"]
                            instructions[workflow_id][task_id]["tools"][tool_index]["marking tool"] = re.sub("'","",label)
                            if tool["details"] != []:
                                instructions[workflow_id][task_id]["tools"][tool_index]["followup_questions"] = {}

                                for subtask_index,subtask in enumerate(tool["details"]):
                                    instructions[workflow_id][task_id]["tools"][tool_index]["followup_questions"][subtask_index] = {}
                                    instructions[workflow_id][task_id]["tools"][tool_index]["followup_questions"][subtask_index]["question"] = subtask["question"]
                                    instructions[workflow_id][task_id]["tools"][tool_index]["followup_questions"][subtask_index]["answers"] = {}
                                    for answer_index,answers in enumerate(subtask["answers"]):
                                        instructions[workflow_id][task_id]["tools"][tool_index]["followup_questions"][subtask_index]["answers"][answer_index] = answers

                # read in when the workflow last went through a major change
                # real problems with subjects that were retired before that date or classifications
                # given for a subject before that date (since the workflow may have changed completely)
                updated_at_timestamps[workflow_id] = individual_workflow["updated_at"]

                # get the MAJOR version number
                versions[workflow_id] = int(math.floor(float(individual_workflow["version"])))

        return workflows,versions,instructions,updated_at_timestamps

    def __get_users_per_cluster__(self,workflow_id,subject_id,task_id,shape):
        """
        return the center of each cluster - for plotting - and associated probability of existing
        :param workflow_id:
        :param subject_id:
        :param task_id:
        :param axes:
        :param threshold:
        :return:
        """
        postgres_cursor = self.postgres_session.cursor()
        # todo - generalize for panoptes
        stmt = "select aggregation from aggregations where workflow_id = " + str(workflow_id) + " and subject_id = '" + str(subject_id) + "'"
        # stmt = "select aggregation from aggregations where subject_id = '" + str(subject_id) + "'"
        postgres_cursor.execute(stmt)

        # todo - this should be a dict but doesn't seem to be - hmmmm :/
        agg = postgres_cursor.fetchone()

        if agg is None:
            print "returning none"
            return {}

        if isinstance(agg[0],str):
            aggregations = json.loads(agg[0])
        else:
            aggregations = agg[0]

        assert isinstance(aggregations,dict)

        users = {}
        for cluster in aggregations[str(task_id)][shape + " clusters"].values():
            if cluster == "cluster_index":
                continue

            center = tuple(cluster["center"])
            print cluster
            users[center] = cluster["users"]
            # # todo - should be only one way - check why both are necessary
            # if isinstance(cluster['existence'][0],dict):
            #     probabilities[center] = cluster['existence'][0]['1']
            # else:
            #     probabilities[center] = cluster['existence'][0][1]

        return users

    # def __get_workflow_versions__(self):#,project_id):
    #     request = urllib2.Request(self.host_api+"workflows?project_id="+str(self.project_id))
    #     # request = urllib2.Request(self.host_api+"workflows/project_id="+str(self.project_id))
    #     request.add_header("Accept","application/vnd.api+json; version=1")
    #     request.add_header("Authorization","Bearer "+self.token)
    #
    #     # request
    #     try:
    #         response = urllib2.urlopen(request)
    #     except urllib2.HTTPError as e:
    #         sys.stderr.write('The server couldn\'t fulfill the request.\n')
    #         sys.stderr.write('Error code: ' + str(e.code) + "\n")
    #         sys.stderr.write('Error response body: ' + str(e.read()) + "\n")
    #         raise
    #     except urllib2.URLError as e:
    #         sys.stderr.write('We failed to reach a server.\n')
    #         sys.stderr.write('Reason: ' + str(e.reason) + "\n")
    #         raise
    #     else:
    #         # everything is fine
    #         body = response.read()
    #
    #     # put it in json structure and extract id
    #     data = json.loads(body)
    #
    #     versions = {}
    #
    #     for workflow in data["workflows"]:
    #         print workflow["version"]
    #
    #         # if int(workflows["id"]) == workflow_id:
    #         versions[int(workflow["id"])] = int(math.floor(float(workflow["version"])))
    #         # versions[int(w["id"])] = w["version"] #int(math.floor(float(w["version"])))
    #     print "+==="
    #     return versions

    def __image_setup__(self,subject_id,download=True):
        """
        get the local file name for a given subject id and downloads that image if necessary
        :param subject_id:
        :return:
        """
        print subject_id
        print self.host_api+"subjects/"+str(subject_id)+"?"
        assert False
        request = urllib2.Request(self.host_api+"subjects/"+str(subject_id)+"?")
        request.add_header("Accept","application/vnd.api+json; version=1")
        request.add_header("Authorization","Bearer "+self.token)
        # request
        try:
            response = urllib2.urlopen(request)
            body = response.read()
        except urllib2.HTTPError as e:
            print self.host_api+"subjects/"+str(subject_id)
            print 'The server couldn\'t fulfill the request.'
            print 'Error code: ', e.code
            print 'Error response body: ', e.read()
            raise
        except urllib2.URLError as e:
            print 'We failed to reach a server.'
            print 'Reason: ', e.reason
            raise

        data = json.loads(body)
        print data
        url = str(data["subjects"][0]["locations"][0]["image/jpeg"])

        slash_index = url.rfind("/")
        fname = url[slash_index+1:]

        image_path = base_directory+"/Databases/images/"+fname

        if not(os.path.isfile(image_path)):
            if download:
                print "downloading"
                urllib.urlretrieve(url, image_path)
            raise ImageNotDownloaded()

        return image_path

    # def __list_all_versions__(self):
    #     request = urllib2.Request(self.host_api+"workflows/6/versions?")
    #     request.add_header("Accept","application/vnd.api+json; version=1")
    #     request.add_header("Authorization","Bearer "+self.token)
    #
    #     # request
    #     try:
    #         response = urllib2.urlopen(request)
    #     except urllib2.HTTPError as e:
    #         print 'The server couldn\'t fulfill the request.'
    #         print 'Error code: ', e.code
    #         print 'Error response body: ', e.read()
    #         raise
    #     except urllib2.URLError as e:
    #         print 'We failed to reach a server.'
    #         print 'Reason: ', e.reason
    #         raise
    #     else:
    #         # everything is fine
    #         body = response.read()
    #
    #     # put it in json structure and extract id
    #     data = json.loads(body)
    #     print data["meta"].keys()
    #     print data["versions"]

    def __load_subjects__(self,workflow_id):
        """
        load the list of subject ids from Cassandra
        :param workflow_id:
        :return:
        """
        # version = int(math.floor(float(self.versions[workflow_id])))
        stmt = "SELECT subject_id FROM subjects WHERE project_id = " + str(self.project_id) + " and workflow_id = " + str(workflow_id)# + " and workflow_version = " + str(version)
        subjects = set([r.subject_id for r in self.cassandra_session.execute(stmt)])

        assert subjects != ()

        return list(subjects)

    def __merge_aggregations__(self,agg1,agg2):
        """
        merge aggregations - could be clustering and classification aggregations
        could be two different clustering aggregations for different shapes
        :param clust_agg:
        :param class_agg:
        :return:
        """
        # start with the clustering results and merge in the classification results
        # assert (agg1 is not None) or (agg2 is not None)
        assert isinstance(agg1,dict) and isinstance(agg2,dict)

        if agg1 == {}:
            return agg2
        elif agg2 == {}:
            return agg1

        for kw in agg2:
            if kw not in agg1:
                agg1[kw] = agg2[kw]
            elif agg1[kw] != agg2[kw]:
                try:
                    agg1[kw] = self.__merge_aggregations__(agg1[kw],agg2[kw])
                except TypeError:
                    print "====-----"
                    print type(agg1)
                    print type(agg2)
                    print agg1
                    print agg2
                    print kw
                    assert False
        return agg1

    def __migrate__(self):
        """
        move data from postgres to cassandra
        :return:
        """
        # try:
        #     self.cassandra_session.execute("drop table classifications")
        #     self.cassandra_session.execute("drop table subjects")
        #     print "table dropped"
        # except cassandra.InvalidRequest:
        #     print "table did not already exist"
        #
        # try:
        #     self.cassandra_session.execute("CREATE TABLE classifications( project_id int, user_id int, workflow_id int, created_at timestamp,annotations text,  updated_at timestamp, user_group_id int, user_ip inet,  completed boolean, gold_standard boolean, subject_id int, workflow_version int,metadata text, PRIMARY KEY(project_id,workflow_id,subject_id,workflow_version,user_ip,user_id) ) WITH CLUSTERING ORDER BY (workflow_id ASC,subject_id ASC,workflow_version ASC,user_ip ASC,user_id ASC);")
        # except cassandra.AlreadyExists:
        #     pass
        #
        # try:
        #     self.cassandra_session.execute("CREATE TABLE subjects (project_id int, workflow_id int, workflow_version int, subject_id int, PRIMARY KEY(project_id,workflow_id,subject_id,workflow_version));")
        # except cassandra.AlreadyExists:
        #     pass

        subject_listing = set()

        select = "SELECT * from classifications where project_id="+str(self.project_id) +" and created_at >= '" + str(self.old_time) +"'"
        cur = self.postgres_session.cursor()
        cur.execute(select)

        # self.migrated_subjects = set()
        print "trying to migrate " + str(self.project_id)
        insert_statement = self.cassandra_session.prepare("""
                insert into classifications (project_id, user_id, workflow_id,  created_at,annotations, updated_at, user_group_id, user_ip, completed, gold_standard, subject_id, workflow_version,metadata)
                values (?,?,?,?,?,?,?,?,?,?,?,?,?)""")

        statements_and_params = []
        migrated = {}
        for ii,t in enumerate(cur.fetchall()):
            print ii
            id_,project_id,user_id,workflow_id,annotations,created_at,updated_at,user_group_id,user_ip,completed,gold_standard,expert_classifier,metadata,subject_ids,workflow_version = t
            # print t
            # assert False
            assert len(subject_ids) == 1
            # self.migrated_subjects.add(subject_ids[0])

            if gold_standard != True:
                gold_standard = False

            if not isinstance(user_group_id,int):
                user_group_id = -1

            if not isinstance(user_id,int):
                user_id = -1
            # get only the major version of the workflow
            workflow_version = int(math.floor(float(workflow_version)))
            id = workflow_id,subject_ids[0]
            # if subject_ids[0] == 4153:
            #     print workflow_id,user_ip

            if id not in migrated:
                migrated[id] = 0
            migrated[id] += 1

            if isinstance(annotations,dict):
                annotations = json.dumps(annotations)
            assert isinstance(annotations,str)

            params = (project_id, user_id, workflow_id,created_at, annotations, updated_at, user_group_id, user_ip,  completed, gold_standard,  subject_ids[0], workflow_version,json.dumps(metadata))
            statements_and_params.append((insert_statement, params))

            # params2 = (project_id,workflow_id,workflow_version,subject_ids[0])
            # statements_and_params.append((insert_statement2,params2))
            subject_listing.add((project_id,workflow_id,workflow_version,subject_ids[0]))

            if len(statements_and_params) == 100:
                results = execute_concurrent(self.cassandra_session, statements_and_params, raise_on_first_error=True)
                statements_and_params = []

        # insert any "left over" classifications
        if statements_and_params != []:
            results = execute_concurrent(self.cassandra_session, statements_and_params, raise_on_first_error=True)

        # now update the subject ids
        statements_and_params = []
        insert_statement = self.cassandra_session.prepare("""
                insert into subjects (project_id,workflow_id,workflow_version,subject_id)
                values (?,?,?,?)""")
        for s in subject_listing:
            statements_and_params.append((insert_statement, s))

            if len(statements_and_params) == 100:
                results = execute_concurrent(self.cassandra_session, statements_and_params, raise_on_first_error=True)
                statements_and_params = []
        if statements_and_params != []:
            results = execute_concurrent(self.cassandra_session, statements_and_params, raise_on_first_error=True)

    def __panoptes_connect__(self,api_details,user_name,password):
        """
        make the main connection to Panoptes - through http
        :return:
        """
        # details for connecting to Panoptes
        # self.user_name = api_details["name"]
        # self.password = api_details["password"]
        self.host = api_details["host"] #"https://panoptes-staging.zooniverse.org/"
        self.host_api = self.host+"api/"
         #"brian-testing" or zooniverse

        self.app_client_id = api_details["app_client_id"]
        # self.environment = api_details["environment"]
        self.token = None

        # the http api for connecting to Panoptes
        self.http_api = None

        for i in range(20):
            try:
                print "connecting to Pantopes, attempt: " + str(i)
                cj = cookielib.CookieJar()
                opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cj))

                #1. get the csrf token

                request = urllib2.Request(self.host+"users/sign_in",None)
                response = opener.open(request)

                body = response.read()
                # grep for csrf-token
                try:
                    csrf_token = re.findall(".+csrf-token.\s+content=\"(.+)\"",body)[0]
                except IndexError:
                    print body
                    raise

                #2. use the token to get a devise session via JSON stored in a cookie
                devise_login_data=("{\"user\": {\"login\":\""+user_name+"\",\"password\":\""+password+
                                   "\"}, \"authenticity_token\": \""+csrf_token+"\"}")

                request = urllib2.Request(self.host+"users/sign_in",data=devise_login_data)
                request.add_header("Content-Type","application/json")
                request.add_header("Accept","application/json")

                try:
                    response = opener.open(request)
                except urllib2.HTTPError as e:
                    print 'In get_bearer_token, stage 2:'
                    print 'The server couldn\'t fulfill the request.'
                    print 'Error code: ', e.code
                    print 'Error response body: ', e.read()
                    raise
                except urllib2.URLError as e:
                    print 'We failed to reach a server.'
                    print 'Reason: ', e.reason
                    raise
                else:
                    # everything is fine
                    body = response.read()

                #3. use the devise session to get a bearer token for API access
                if self.app_client_id != "":
                    bearer_req_data=("{\"grant_type\":\"password\",\"client_id\":\"" + self.app_client_id + "\"}")
                else:
                    bearer_req_data=("{\"grant_type\":\"password\"}")
                request = urllib2.Request(self.host+"oauth/token",bearer_req_data)
                request.add_header("Content-Type","application/json")
                request.add_header("Accept","application/json")

                try:
                    response = opener.open(request)
                except urllib2.HTTPError as e:
                    print 'In get_bearer_token, stage 3:'
                    print 'The server couldn\'t fulfill the request.'
                    print 'Error code: ', e.code
                    print 'Error response body: ', e.read()
                    raise
                except urllib2.URLError as e:
                    print 'We failed to reach a server.'
                    print 'Reason: ', e.reason
                    raise
                else:
                    # everything is fine
                    body = response.read()

                # extract the bearer token
                json_data = json.loads(body)
                bearer_token = json_data["access_token"]

                self.token = bearer_token
                break
            except (urllib2.HTTPError,urllib2.URLError) as e:
                print "trying to connect/init again again"
                pass

    def __panoptes_aggregation__(self):

        # request = urllib2.Request(self.host_api+"aggregations?workflow_id="+str(2)+"&subject_id="+str(458021)+"&admin=true")
        request = urllib2.Request(self.host_api+"aggregations?workflow_id="+str(2)+"&admin=true")
        print self.host_api+"aggregations?workflow_id="+str(2)+",subject_id="+str(458021)
        # request = urllib2.Request(self.host_api+"workflows/project_id="+str(self.project_id))
        request.add_header("Accept","application/vnd.api+json; version=1")
        request.add_header("Authorization","Bearer "+self.token)

        # request
        try:
            response = urllib2.urlopen(request)
        except urllib2.HTTPError as e:
            sys.stderr.write('The server couldn\'t fulfill the request.\n')
            sys.stderr.write('Error code: ' + str(e.code) + "\n")
            sys.stderr.write('Error response body: ' + str(e.read()) + "\n")
            raise
        except urllib2.URLError as e:
            sys.stderr.write('We failed to reach a server.\n')
            sys.stderr.write('Reason: ' + str(e.reason) + "\n")
            raise
        else:
            # everything is fine
            body = response.read()

        # put it in json structure and extract id
        data = json.loads(body)

        print data


    def __plot_image__(self,subject_id,axes):
        # todo - still learning about Matplotlib and axes
        # see http://matplotlib.org/users/artists.html
        fname = self.__image_setup__(subject_id)

        for i in range(10):
            try:
                # fig = plt.figure()
                # ax = fig.add_subplot(1, 1, 1)
                image_file = cbook.get_sample_data(fname)
                image = plt.imread(image_file)
                # fig, ax = plt.subplots()
                im = axes.imshow(image)

                return self.__get_subject_dimension__(subject_id)
            except IOError:
                # try downloading that image again
                os.remove(fname)
                self.__image_setup__(subject_id)

        assert False

    # def __plot_individual_points__(self,subject_id,task_id,shape):
    #     for cluster in self.cluster_alg.clusterResults[task_id][shape][subject_id]:
    #         for pt in cluster["points"]:
    #             if shape == "line":
    #                 plt.plot([pt[0],pt[1]],[pt[2],pt[3]],color="red")
    #             elif shape == "point":
    #                 plt.plot([pt[0]],[pt[1]],".",color="red")
    #             elif shape == "circle":
    #                 print (pt[0],pt[1]),pt[2]
    #                 e = Ellipse((pt[0],pt[1]),width = pt[2],height=pt[2],fill=False,color="red")
    #                 # circle = plt.Circle((pt[0],pt[1]),pt[2],color=cnames.values()[users.index(user_id)])
    #                 plt.gca().add_patch(e)
    #                 # ax.add_artist(e)
    #                 # e.set_alpha(0)
    #             elif shape == "ellipse":
    #                 # ("angle","rx","ry","x","y")
    #                 e = Ellipse((pt[3],pt[4]),width = pt[2],height=pt[1],fill=False,angle=pt[0],color="red")
    #             elif shape == "rectangle":
    #                 plt.plot([pt[0],pt[0]+pt[2]],[pt[1],pt[1]],color="red")
    #                 plt.plot([pt[0],pt[0]],[pt[1],pt[1]+pt[3]],color="red")
    #                 plt.plot([pt[0]+pt[2],pt[0]+pt[2]],[pt[1],pt[1]+pt[3]],color="red")
    #                 plt.plot([pt[0],pt[0]+pt[2]],[pt[1]+pt[3],pt[1]+pt[3]],color="red")
    #             else:
    #                 print shape
    #                 assert False
    #
    #     plt.axis('scaled')

    # def __get_cluster_markings__(self,workflow_id,subject_id,task_id,shape,axes,percentile_threshold=None,correct_pts=None,incorrect_pts=None):
    def __get_cluster_markings__(self,workflow_id,subject_id,task_id,shape):
        """
        return the center of each cluster - for plotting - and associated probability of existing
        :param workflow_id:
        :param subject_id:
        :param task_id:
        :param axes:
        :param threshold:
        :return:
        """
        postgres_cursor = self.postgres_session.cursor()
        # todo - generalize for panoptes
        stmt = "select aggregation from aggregations where workflow_id = " + str(workflow_id) + " and subject_id = '" + str(subject_id) + "'"
        # stmt = "select aggregation from aggregations where subject_id = '" + str(subject_id) + "'"
        postgres_cursor.execute(stmt)

        # todo - this should be a dict but doesn't seem to be - hmmmm :/
        agg = postgres_cursor.fetchone()

        if agg is None:
            print "returning none"
            return {}

        if isinstance(agg[0],str):
            aggregations = json.loads(agg[0])
        else:
            aggregations = agg[0]

        assert isinstance(aggregations,dict)

        probabilities = {}
        for cluster in aggregations[str(task_id)][shape + " clusters"].values():
            if cluster == "cluster_index":
                continue

            center = tuple(cluster["center"])

            # todo - should be only one way - check why both are necessary
            if isinstance(cluster['existence'][0],dict):
                probabilities[center] = cluster['existence'][0]['1']
            else:
                probabilities[center] = cluster['existence'][0][1]

        return probabilities




    # def __plot__(self,workflow_id,task_id):
    #     print "plotting"
    #     try:
    #         print "----"
    #         for shape in self.cluster_alg.clusterResults[task_id]:
    #             for subject_id in self.cluster_alg.clusterResults[task_id][shape]:
    #                 print subject_id
    #                 if (len(self.users_per_subject[subject_id]) >= 1):# and (subject_id in self.classification_alg.results):
    #                     # if self.cluster_alg.clusterResults[task][shape][subject_id]["users"]
    #                     self.__plot_image__(subject_id)
    #                     self.__plot_individual_points__(subject_id,task_id,shape)
    #                     # self.__plot_cluster_results__(subject_id,task,shape)
    #
    #                     if (self.classification_alg is not None) and (subject_id in self.classification_alg.results):
    #                         classification_task = "init"
    #                         classifications = self.classification_alg.results[subject_id][classification_task]
    #                         # print classifications
    #                         votes,total = classifications
    #                         title = self.description[classification_task][0]
    #                         # print self.description
    #                         for answer_index,percentage in votes.items():
    #                             if title != "":
    #                                 title += "\n"
    #                             title += self.description[classification_task][answer_index+1] + ": " + str(int(percentage*total))
    #                         # print  self.description[classification_task][0]
    #                         # print title
    #
    #                         plt.title(title)
    #                     plt.title("number of users: " + str(len(self.users_per_subject[subject_id][task_id])))
    #                     plt.savefig("/home/greg/Databases/"+self.project_short_name+"/markings/"+str(subject_id)+".jpg")
    #                     plt.close()
    #                     # assert False
    #     except KeyError as e:
    #         print self.cluster_alg.clusterResults.keys()
    #         raise

    def __postgres_connect__(self,database_details):

        details = ""

        details += "dbname = '" +database_details["database"] +"'"
        details += " user = '" + database_details["username"] + "'"

        # if no password is provided - hopefully connecting to the local cluster
        try:
            # password = database_details["password"]
            details += " password = '"+database_details["password"]+"' "
        except KeyError:
            pass

        try:
            details += " host ='" + database_details["host"] +"'"
        except KeyError:
            pass
        # host = database_details["host"]

        for i in range(20):
            try:
                self.postgres_session = psycopg2.connect(details)
                # self.postgres_cursor = self.postgres_session.cursor()
                break
            except psycopg2.OperationalError as e:
                print e
                pass

        if self.postgres_session is None:
            raise psycopg2.OperationalError()

        cursor = self.postgres_session.cursor()

        # select = "SELECT * from project_contents"
        # cur = self.postgres_session.cursor()
        # cur.execute(select)

    # def __postgres_backup__(self):
    #     local_session = psycopg2.connect("dbname='tate' user='panoptes' host='localhost' password='apassword'")
    #     local_cursor = local_session.cursor()
    #
    #     # local_cursor.execute("CREATE TABLE annotate_classifications (project_id integer,user_id integer,workflow_id integer,annotations text,created_at timestamp,updated_at timestamp,user_group_id integer, user_ip inet, completed boolean, gold_standard boolean, expert_classifier integer, metadata text, subject_ids integer, workflow_version text);")
    #     local_cursor.execute("CREATE TABLE annotate_classifications (annotations json);")
    #
    #     select = "SELECT * from classifications where project_id="+str(self.project_id)
    #     cur = self.postgres_session.cursor()
    #     cur.execute(select)
    #
    #     for ii,t in enumerate(cur.fetchall()):
    #         id_,project_id,user_id,workflow_id,annotations,created_at,updated_at,user_group_id,user_ip,completed,gold_standard,expert_classifier,metadata,subject_ids,workflow_version = t
    #         print type(json.dumps(annotations))
    #         # local_cursor.execute("""
    #         #     INSERT INTO annotate_classifications
    #         #     (project_id,user_id,workflow_id,annotations,created_at,updated_at,user_group_id, user_ip, completed, gold_standard, expert_classifier, metadata, subject_id, workflow_version)
    #         #     values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
    #         #     (id_,project_id,user_id,workflow_id,json.dumps(annotations),created_at,updated_at,user_group_id,user_ip,completed,gold_standard,expert_classifier,json.dumps(metadata),subject_ids[0],workflow_version))
    #         st = json.dumps(annotations)
    #         local_cursor.execute("""
    #             INSERT INTO annotate_classifications
    #             (annotations)
    #             values (%s)""",
    #             (annotations))

    def __prune__(self,aggregations):
        """
        remove unnecessary information from the json file to keep things reasonable
        :param aggregations:
        :return:
        """
        assert isinstance(aggregations,dict)
        for task_id in aggregations:
            if task_id == "param":
                continue

            if isinstance(aggregations[task_id],dict):
                for cluster_type in aggregations[task_id]:
                    if cluster_type == "param":
                        continue

                    del aggregations[task_id][cluster_type]["all_users"]
                    for cluster_index in aggregations[task_id][cluster_type]:
                        if cluster_index == "param":
                            continue

                        # del aggregations[task_id][cluster_type][cluster_index]["cluster members"]
                        del aggregations[task_id][cluster_type][cluster_index]["tools"]
                        # del aggregations[task_id][cluster_type][cluster_index]["users"]

        return aggregations

    def __readin_tasks__(self,workflow_id):
        """
        get the details for each task - for example, what tasks might we want to run clustering algorithms on
        and if so, what params related to that task are relevant
        :return:
        """
        # get the tasks associated with the given workflow
        select = "SELECT tasks from workflows where id = " + str(workflow_id)
        cursor = self.postgres_session.cursor()
        cursor.execute(select)
        tasks = cursor.fetchone()[0]

        # which of these tasks have classifications associated with them?
        classification_tasks = {}
        # which have drawings associated with them
        marking_tasks = {}

        # convert to json is necessary - not sure why this is necessary but it does happen
        # see https://github.com/zooniverse/aggregation/issues/7
        if isinstance(tasks,str):
            tasks = json.loads(tasks)

        print "=====//"

        for task_id in tasks:
            # self.task_type[task_id] = tasks[task_id]["type"]
            # if the task is a drawing one, get the necessary details for clustering
            if tasks[task_id]["type"] == "drawing":
                marking_tasks[task_id] = []
                # manage marking tools by the marking type and not the index
                # so all ellipses will be clustered together

                # # see if mulitple tools are creating the same shape
                # counter = {}

                for tool in tasks[task_id]["tools"]:
                    # shape = ellipse, line, pt etc.
                    shape = tool["type"]
                    print shape

                    # extract the label of the tool - this means that things don't have to ordered
                    label = tool["label"]
                    label_words = label.split(".")
                    tool_id = int(label_words[2])

                    # are there any classification questions associated with this marking?

                    if ("details" in tool) and (tool["details"] is not None) and (tool["details"] != []):
                        if task_id not in classification_tasks:
                            classification_tasks[task_id] = {}
                        if "subtask" not in classification_tasks[task_id]:
                            classification_tasks[task_id]["subtask"] = {}
                        if tool_id not in classification_tasks[task_id]["subtask"]:
                            classification_tasks[task_id]["subtask"][tool_id] = range(len(tool["details"]))
                        # if tool_id not in self.classification_tasks[task_id]:
                        #     self.classification_tasks[task_id][tool_id] = {}
                        # classification_tasks[task_id][tool_id]= [i for i in range(len(tool["details"]))]
                        # todo - fix this

                    if tool["type"] == "line":
                        marking_tasks[task_id].append("line")
                    elif tool["type"] == "ellipse":
                        marking_tasks[task_id].append("ellipse")
                    elif tool["type"] == "point":
                        marking_tasks[task_id].append("point")
                    elif tool["type"] == "circle":
                        marking_tasks[task_id].append("circle")
                    elif tool["type"] == "rectangle":
                        marking_tasks[task_id].append("rectangle")
                    elif tool["type"] == "polygon":
                        marking_tasks[task_id].append("polygon")
                    else:
                        print tool
                        assert False

                    if task_id not in classification_tasks:
                        classification_tasks[task_id] = {}
                    if "shapes" not in classification_tasks[task_id]:
                        classification_tasks[task_id]["shapes"] = []

                    classification_tasks[task_id]["shapes"].append(shape)

            else:
                # self.marking_params_per_task[task_id] = []
                classification_tasks[task_id] = True

        # find out if any of the shapes for a given task are "confusing"
        # that is more, there is more than 1 tool which can create that shape
        for task_id in marking_tasks:
            for shape in ["line","ellipse","point","circle","rectangle"]:
                if sum([1 for s in marking_tasks[task_id] if s == shape]) > 1:
                    # this shape is confusing
                    if task_id not in classification_tasks:
                        classification_tasks[task_id] = {}
                    if "confusing shapes" not in classification_tasks[task_id]:
                        classification_tasks[task_id]["confusing shapes"] = []

                    classification_tasks[task_id]["confusing shapes"].append(shape)


        return classification_tasks,marking_tasks

    def __roi_check__(self,marking,subject_id):
        """
        some projects may have a region of interest in which all markings are supposed to lie
        since things never go as planned, some marking may be outside of the roi
        in which case these markings should be rejected
        """
        return True

    # def __remove_user_ids__(self,aggregation):
    #     """
    #     ids are needed for aggregation but they shouldn't be stored with the results
    #     NOTE ids are postgres ids, NOT ip or email addresses
    #     """
    #     for subject_id in aggregation:
    #         if subject_id == "param":
    #             continue
    #
    #         for task_id in aggregation[subject_id]:
    #             if task_id == "param":
    #                 continue
    #             if isinstance(aggregation[subject_id][task_id],dict):
    #                 for shape in aggregation[subject_id][task_id]:
    #                     if shape == "param":
    #                         continue
    #
    #                     for cluster_index in aggregation[subject_id][task_id][shape]:
    #                         if cluster_index == "param":
    #                             continue
    #
    #                         assert isinstance(aggregation[subject_id][task_id][shape][cluster_index],dict)
    #                         #aggregation[subject_id][task_id][shape][cluster_index].pop("users",None)
    #
    #                         del aggregation[subject_id][task_id][shape][cluster_index]["users"]
    #
    #     return aggregation

    def __get_results__(self,workflow_id):
        stmt = "select * from aggregations where workflow_id = " + str(workflow_id)
        self.postgres_cursor.execute(stmt)
        for r in self.postgres_cursor.fetchall():
            return r

    def __results_to_file__(self,workflow_ids=None,subject_id=None):
        if workflow_ids is None:
            workflow_ids = self.workflows.keys()
        elif isinstance(workflow_ids,int):
            workflow_ids = [workflow_ids]

        assert isinstance(workflow_ids,list)

        for id_ in workflow_ids:
            print "storing workflow id :: " + str(id_)
            stmt = "select * from aggregations where workflow_id = " + str(id_)
            if subject_id is not None:
                stmt += " and subject_id = " + str(subject_id)
            self.postgres_cursor.execute(stmt)
            all_results = []
            for r in self.postgres_cursor.fetchall():
                assert isinstance(r[3],dict)

                ordered_aggregations = OrderedDict(sorted(r[3].items(),key = lambda x:x[0]))
                results = {r[2]:ordered_aggregations}
                all_results.append(results)
            with open('/home/greg/workflow'+str(id_)+'.json', 'w') as outfile:
                # for reasoning see
                # http://stackoverflow.com/questions/18871217/python-how-to-custom-sort-a-list-of-dict-to-use-in-json-dumps
                json.dump(all_results, outfile,sort_keys=True,indent=4, separators=(',', ': '))

    def __set_classification_alg__(self,alg):
        self.classification_alg = alg()
        assert isinstance(self.classification_alg,classification.Classification)

    def __set_clustering_algs__(self,clustering_algorithms,reduction_algs={}):

        # the dictionary allows us to give a different clustering algorithm for different shapes

        self.cluster_algs = {}
        assert isinstance(clustering_algorithms,dict)
        for shape in clustering_algorithms:
            assert shape in self.marking_params_per_shape
            print shape
            if shape in reduction_algs:
                self.cluster_algs[shape] = clustering_algorithms[shape](shape,reduction_algs[shape])
            else:
                self.cluster_algs[shape] = clustering_algorithms[shape](shape,identity_mapping)
            assert isinstance(self.cluster_algs[shape],clustering.Cluster)

    # def __setup_workflows__(self):#,project_id):
    #     request = urllib2.Request(self.host_api+"workflows?project_id="+str(self.project_id))
    #     request.add_header("Accept","application/vnd.api+json; version=1")
    #     request.add_header("Authorization","Bearer "+self.token)
    #
    #     # request
    #     try:
    #         response = urllib2.urlopen(request)
    #     except urllib2.HTTPError as e:
    #         print 'The server couldn\'t fulfill the request.'
    #         print 'Error code: ', e.code
    #         print 'Error response body: ', e.read()
    #         raise
    #     except urllib2.URLError as e:
    #         print 'We failed to reach a server.'
    #         print 'Reason: ', e.reason
    #         raise
    #     else:
    #         # everything is fine
    #         body = response.read()
    #
    #     # put it in json structure and extract id
    #     data = json.loads(body)
    #
    #     # for each workflow, read in the tasks
    #     workflows = {}
    #
    #     for individual_workflow in data["workflows"]:
    #         id_ = int(individual_workflow["id"])
    #         print individual_workflow["updated_at"]
    #         assert False
    #         # self.workflows.append(id_)
    #         workflows[id_] = self.__readin_tasks__(id_)
    #         # self.subject_sets[id_] = self.__get_subject_ids__(id_)
    #
    #     # assert False
    #     # read in the most current version of each of the workflows
    #     return workflows

    def __sort_annotations__(self,workflow_id,subject_set=None,expert=None):
        """
        experts is when you have experts for whom you don't want to read in there classifications
        :param workflow_id:
        :param subject_set:
        :param experts:
        :return:
        """

        version = int(math.floor(float(self.versions[workflow_id])))

        # todo - do this better
        width = 2000
        height = 2000

        classification_tasks,marking_tasks = self.workflows[workflow_id]
        raw_classifications = {}
        raw_markings = {}

        if subject_set is None:
            subject_set = self.__load_subjects__(workflow_id)

        total = 0

        # do this in bite sized pieces to avoid overwhelming DB
        for s in self.__chunks(subject_set,15):
            statements_and_params = []

            if self.ignore_versions:
                select_statement = self.cassandra_session.prepare("select user_id,annotations,workflow_version from "+self.classification_table+" where project_id = ? and subject_id = ? and workflow_id = ?")
            else:
                select_statement = self.cassandra_session.prepare("select user_id,annotations,workflow_version from "+self.classification_table+" where project_id = ? and subject_id = ? and workflow_id = ? and workflow_version = ?")

            for subject_id in s:
                if self.ignore_versions:
                    params = (int(self.project_id),subject_id,int(workflow_id))
                else:
                    params = (int(self.project_id),subject_id,int(workflow_id),version)
                statements_and_params.append((select_statement, params))
            results = execute_concurrent(self.cassandra_session, statements_and_params, raise_on_first_error=False)

            for subject_id,(success,record_list) in zip(s,results):

                if not success:
                    print record_list
                assert success

                print subject_id



                # seem to have the occasional "retired" subject with no classifications, not sure
                # why this is possible but if it can happen, just make a note of the subject id and skip
                if record_list == []:
                    print "warning :: subject " + str(subject_id) + " has no classifications"
                    continue

                non_logged_in_users = 0
                for record in record_list:

                    total += 1
                    user_id = record.user_id

                    if user_id == expert:
                        continue

                    # todo - maybe having user_id=="" would be useful for penguins
                    if (user_id == -1):
                        non_logged_in_users += -1
                        user_id = non_logged_in_users

                    annotations = json.loads(record.annotations)

                    # go through each annotation and get the associated task
                    for task in annotations:
                        task_id = task["task"]

                        # is this a marking task?
                        if task_id in marking_tasks:
                            # if a user gets to marking task but makes no markings, we want to record that the user
                            # has still seen that image/task. If a user never gets to a marking task for that image
                            # than they are irrelevant
                            # create here so even if we have empty images, we will know that we aggregated them
                            # make sure to not overwrite/delete existing information - sigh
                            if task_id not in raw_markings:
                                raw_markings[task_id] = {}
                            for shape in set(marking_tasks[task_id]):
                                if shape not in raw_markings[task_id]:
                                    raw_markings[task_id][shape] = {}
                                if subject_id not in raw_markings[task_id][shape]:
                                    raw_markings[task_id][shape][subject_id] = []

                            if not isinstance(task["value"],list):
                                print "not properly formed marking - skipping"
                                continue

                            # kind track of which shapes the user did mark - we need to keep track of any shapes
                            # for which the user did not make any marks at all of
                            # because a user not seeing something is important
                            spotted_shapes = set()

                            for marking in task["value"]:
                                # what kind of tool made this marking and what was the shape of that tool?
                                try:
                                    tool = marking["tool"]
                                    shape = marking_tasks[task_id][tool]
                                except KeyError:
                                    tool = None
                                    shape = marking["type"]

                                if shape ==  "image":
                                    # todo - treat image like a rectangle
                                    print "image found - skipping"
                                    continue

                                if shape not in self.marking_params_per_shape:
                                    print "unrecognized shape: (skipping) " + shape
                                    continue

                                try:
                                    # extract the params specifically relevant to the given shape
                                    relevant_params = self.marking_params_per_shape[shape](marking,(width,height))
                                except InvalidMarking as e:
                                    # print e
                                    continue
                                # if subject_id == "APZ0002do1":
                                #     print  self.__roi_check__(marking,subject_id)
                                # if not a valid marking, just skip it
                                if not self.__roi_check__(marking,subject_id):
                                    continue

                                spotted_shapes.add(shape)
                                raw_markings[task_id][shape][subject_id].append((user_id,relevant_params,tool))

                                # is this a confusing shape?
                                if (task_id in classification_tasks) and ("shapes" in classification_tasks[task_id]) and (shape in classification_tasks[task_id]["shapes"]):
                                    if task_id not in raw_classifications:
                                        raw_classifications[task_id] = {}
                                    if shape not in raw_classifications[task_id]:
                                        raw_classifications[task_id][shape] = {}
                                    if subject_id not in raw_classifications[task_id][shape]:
                                        raw_classifications[task_id][shape][subject_id] = {}

                                    raw_classifications[task_id][shape][subject_id][(relevant_params,user_id)] = tool

                                # are there follow up questions?
                                if (task_id in classification_tasks) and ("subtask" in classification_tasks[task_id]) and (tool in classification_tasks[task_id]["subtask"]):

                                    # there could be multiple follow up questions
                                    for local_subtask_id in classification_tasks[task_id]["subtask"][tool]:
                                        global_subtask_id = str(task_id)+"_"+str(tool)+"_"+str(local_subtask_id)
                                        if global_subtask_id not in raw_classifications:
                                            raw_classifications[global_subtask_id] = {}
                                        if subject_id not in raw_classifications[global_subtask_id]:
                                            raw_classifications[global_subtask_id][subject_id] = {}

                                        # # specific tool matters, not just shape
                                        subtask_value = marking["details"][local_subtask_id]["value"]
                                        # if tool not in raw_classifications[global_subtask_id][subject_id]:
                                        #     raw_classifications[global_subtask_id][subject_id][tool] = {}
                                        raw_classifications[global_subtask_id][subject_id][(relevant_params,user_id)] = subtask_value

                            # note which shapes the user saw nothing of
                            # otherwise, it will be as if the user didn't see the subject in the first place

                            for shape in set(marking_tasks[task_id]):
                                if shape not in spotted_shapes:
                                    raw_markings[task_id][shape][subject_id].append((user_id,None,None))

                        # we a have a pure classification task
                        else:
                            if task_id not in raw_classifications:
                                raw_classifications[task_id] = {}
                            if subject_id not in raw_classifications[task_id]:
                                raw_classifications[task_id][subject_id] = []
                            # if task_id == "init":
                            #     print task_id,task["value"]
                            raw_classifications[task_id][subject_id].append((user_id,task["value"]))

        # for debugging only
        for task_id in raw_markings:
            # go through each shape independently
            for shape in raw_markings[task_id].keys():
                for subject_id in raw_markings[task_id][shape]:
                    print subject_id
                    assert raw_markings[task_id][shape][subject_id] != []

        return raw_classifications,raw_markings

    def __threshold_scaling__(self,workflow_id,):
        """
        when moving the threshold, we don't want a linear scale - most values will be uninteresting
        so use a few subjects to create a more interesting scale
        :return:
        """
        stmt = "select subject_id from aggregations where workflow_id = " + str(workflow_id)
        self.postgres_cursor.execute(stmt)

        subjects = []

        for r in self.postgres_cursor.fetchall():
            subjects.append(r[0])

        return subjects


    def __upsert_results__(self,workflow_id,aggregations):
        """
        see
        # http://stackoverflow.com/questions/8134602/psycopg2-insert-multiple-rows-with-one-query
        :param workflow_id:
        :param aggregations:
        :return:
        """
        # subject_ids = [id_ for id_ in aggregations if id_ != "param"]
        # cur.executemany("""INSERT INTO bar(first_name,last_name) VALUES (%(first_name)s, %(last_name)s)""", namedict)

        # self.postgres_cursor.execute("CREATE TEMPORARY TABLE newvals(workflow_id int, subject_id int, aggregation jsonb, created_at timestamp, updated_at timestamp)")
        postgres_cursor = self.postgres_session.cursor()

        try:
            postgres_cursor.execute("CREATE TEMPORARY TABLE newvals(workflow_id int, subject_id " + self.subject_id_type+ ", aggregation jsonb)")
        except psycopg2.ProgrammingError as e:
            # todo - the table should always be deleted after its use, so this should rarely happen
            # todo - need to reset the connection
            print e
            self.postgres_session.rollback()
            postgres_cursor = self.postgres_session.cursor()

        try:
            postgres_cursor.execute("select subject_id from aggregations where workflow_id = " + str(workflow_id))
            r = [i[0] for i in postgres_cursor.fetchall()]
        except psycopg2.ProgrammingError:
            self.postgres_session.rollback()
            postgres_cursor = self.postgres_session.cursor()
            postgres_cursor.execute("create table aggregations(workflow_id int, subject_id " + self.subject_id_type+ ", aggregation json,created_at timestamp, updated_at timestamp)")
            r = []

        # if self.host_api is not None:
        #     workflow_details = self.__get_workflow_details__(workflow_id)
        # else:
        #     workflow_details = ""

        update_str = ""
        insert_str = ""

        update_counter = 0
        insert_counter = 0

        # todo - sort the subject ids so that searching is faster
        for subject_id in aggregations:
            # todo - maybe get rid of param in subject_ids - end users won't see it anyways
            if subject_id == "param":
                continue

            # todo - put the pruning back, this is a stop gap measure so I know how many users marked each cluster
            # aggregation = aggregations[subject_id]
            aggregation = self.__prune__(aggregations[subject_id])
            # aggregation[" metadata"] = metadata

            # try to add instructions, if none are provided, don't worry
            try:
                aggregation[" instructions"] = self.instructions[workflow_id]
            except KeyError:
                pass

            if subject_id in r:
                # we are updating
                update_str += ","+postgres_cursor.mogrify("(%s,%s,%s)", (workflow_id,subject_id,json.dumps(aggregation)))
                update_counter += 1
            else:
                # we are inserting a brand new aggregation
                insert_str += ","+postgres_cursor.mogrify("(%s,%s,%s,%s,%s)", (workflow_id,subject_id,json.dumps(aggregation),str(datetime.datetime.now()),str(datetime.datetime.now())))
                insert_counter += 1

        if update_str != "":
            # are there any updates to actually be done?
            # todo - the updated and created at dates are not being maintained - I'm happy with that
            print "updating " + str(update_counter) + " subjects"
            postgres_cursor.execute("INSERT INTO newvals (workflow_id, subject_id, aggregation) VALUES " + update_str[1:])
            postgres_cursor.execute("UPDATE aggregations SET aggregation = newvals.aggregation FROM newvals WHERE newvals.subject_id = aggregations.subject_id and newvals.workflow_id = aggregations.workflow_id")
        if insert_str != "":
            print "inserting " + str(insert_counter) + " subjects"
            postgres_cursor.execute("INSERT INTO aggregations (workflow_id, subject_id, aggregation, created_at, updated_at) VALUES " + insert_str[1:])
        self.postgres_session.commit()



class SubjectGenerator:
    def __init__(self,project):
        self.project = project

    def __iter__(self):
        subject_ids = []
        for subject in self.project.subject_collection.find():
            subject_ids.append(subject["zooniverse_id"])

            if len(subject_ids) == 50:
                yield subject_ids
                subject_ids = []

        yield  subject_ids
        raise StopIteration


# def twod_linesegment(pt):
#     x1,x2,y1,y2 = pt
#     print x1,x2,y1,y2
#     dist = (x2*y1-y2*x1)/math.sqrt((y2-y1)**2+(x2-x1)**2)
#
#     try:
#         tan_theta = math.fabs(y1-y2)/math.fabs(x1-x2)
#         theta = math.atan(tan_theta)
#     except ZeroDivisionError:
#         theta = math.pi/2.
#
#     return dist,theta

if __name__ == "__main__":
    project_identifier = sys.argv[1]
    if len(sys.argv) > 2:
        environment = sys.argv[2]
    else:
        environment = 'production'
    with AggregationAPI(project_identifier,environment) as project:
        project.__migrate__()
        # print json.dumps(project.__aggregate__(store_values=False)[464952], sort_keys=True, indent=4, separators=(',', ': '))
        print project.__aggregate__()#workflows=[84],subject_set=[494900])#,subject_set=[495225])#subject_set=[460208, 460210, 460212, 460214, 460216])
        # project.__panoptes_aggregation__()
        project.__csv_output__()#workflow_ids =[84],subject_id=494900)
    # project.__get_workflow_details__(84)
