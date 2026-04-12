# ruff: noqa: F541, F841
import time as te
import oracledb as cx_Oracle
import pandas as pd
from pandas import DataFrame
from os.path import join
from os import getcwd
import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay
from pathlib import Path
import urllib3
import shutil
import logging
_logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
us_bus = CustomBusinessDay(calendar=USFederalHolidayCalendar())

start = te.time()

# bin folder of your Oracle 64 install. Should be the same for everyone
oracle_64bit_path = r"C:\\Oracle64\\product\\19.0.0\\client_2\\bin"
cx_Oracle.init_oracle_client(lib_dir=oracle_64bit_path)
# tns definition
hop1_tns = cx_Oracle.makedsn("cbfabec902-scan.msad.crbgf.net", "1521", service_name ="scbhop1.msad.crbgf.net")
db = "hop1"    #database environment.
username = 'zhizhang' # Database Username
pwd = 'i2bq#4549' # Database Password


def exec_query(script):
    connection = cx_Oracle.connect(user=username,
                                   password = pwd,
                                   dsn = hop1_tns)
    cur = connection.cursor()
    cur.execute(script)
    data, header = cur.fetchall(), [i[0] for i in cur.description]
    cur.close()
    connection.close()
    return data, header
    
def read_script(path):
    with open(path) as SQLfile:
        script = SQLfile.read()
        SQLfile.close()
    return script
    
    
            
def retrieve_data_from_Navisys(cycle_date: str, outPath: Path, df_queries: pd.DataFrame):
            # Don't retrieve data if data already exist in designated foler
            # if os.path.exists(outPath) and len(os.listdir(outPath)) >= 10:
            #     _logger.info(f"Data already exist. Data loader not initiated...")
            #     return
            
            os.makedirs(outPath, exist_ok=True)

            query_folder = join(getcwd(), "Query") 
            for _, row in df_queries.iterrows():
                # Access columns by name
                item = row['Items']
                query_file = row['Query Script']
                output_csv_name = row['SQL Output File Name']
                overwrite_bool = row['Overwrite']
                
                if (os.path.exists(os.path.join(outPath, output_csv_name)) and overwrite_bool or
                    (not os.path.exists(os.path.join(outPath, output_csv_name)))):
                    txn_script = read_script(join(query_folder, query_file)) 
                    # extra query edit for main transaction data    
                    if item in {"Main transaction data", "Main transaction data_Step1", "Main transaction data_Final", "Daily OUV values"}:
                        # dynamic cycle date based on user input if available
                        if cycle_date is not None:
                            txn_script = txn_script.replace(r'/*CYCLE_Date*/', cycle_date)
                        # otherwise use Business day - 2 date as cycle date filter
                        else:
                            index_val = pd.read_csv(Path(outPath) / 'Index_Values.csv')
                            prior_max_Date = index_val.iloc[-2]['IV_INDEX_DATE']
                            txn_script = txn_script.replace(r'/*CYCLE_Date*/', prior_max_Date)
                    # extra query edit for main RILA autolock OUV data
                    elif query_file ==  "RILA_OUV_AL.sql":
                        batch_size = 800
                        df_pol = pd.read_csv(Path(outPath) / 'Allocation_Auto_Lock_Policies.csv')
                        pol_list = df_pol.loc[0:batch_size, "PC_CONT"].apply(lambda x: "'" + x + "'").to_list()
                        pol_str = ",".join(pol_list)
                        txn_script = txn_script.replace(r'/*POLICY_NUMBERS*/', pol_str)
                    data, header = exec_query(txn_script)
                    indx_df_txn, indx_df_txn.columns = DataFrame(data), header
                    appended_data = []
                    appended_data.append(indx_df_txn)
                    appended_data = pd.concat(appended_data)    
                    appended_data.to_csv(Path(outPath) / output_csv_name,index = False)
                    _logger.info(f"Extracting {item} finished..")
                else:
                    _logger.info(f"Data already exists and user opts for no overwrite. {item} not loaded...")

            sc_path = r"C:\AAA_RILA_Validation_Modernization\RILA_Python\Input_RILA\RILA_SC.csv"
            shutil.copy2(sc_path, Path(outPath) / "RILA_SC.csv")
            # excel_prog_path = r"C:\AAA_RILA_Validation_Modernization\RILA_Python\Input_RILA\RILA_Phase2_03222026_itereate_result.xlsm"
            # shutil.copy2(excel_prog_path, Path(outPath) / "RILA_Phase2_03222026_itereate_result.xlsm")
            
                        
def FO_Scrapper(input_folder = None, credentials = None):
    '''
    Scraping front office for certain values that are produced daily (only available on FO)
    Querying Navisys can only get you values on txn dates
    '''
    
    if os.path.exists(Path(input_folder) / "FO_WebScrapped_Data.csv") :
        _logger.info(f"Scrapped data already exists. Web scrapper not initiated...")
        return
    
    _logger.info(f"Web scrapper started...")
    count = 0
    secure_url = ('https://live.fo.annuities.crbgf.net/en/account/AccountWelcome.jsp')
    account_into = ('https://live.fo.annuities.crbgf.net/en/account/AccountQuickView.jsp?Go=Yes')
    cvq = ('https://live.fo.annuities.crbgf.net/en/account/AccountCashValueQuote.jsp')
    policy_data = pd.DataFrame(columns=['Processed Date', 'Policy Number', 'CashSurrenderValue','MinimumWithdrawalValue','AccumulatedValue:', 'SurrenderPenalty', 'TotalDeathBenefit:'])
    csv_Str = 'CashSurrenderValue:'
    cred_payload = {
        'username': 'zhizhang', #Update Navisys Username
        'password': '@Ppleon88013101'  #Update Navisys Password
        }

    policy_GMDB = pd.read_csv(Path(input_folder) / 'migmdbrila1.csv')
    index_val = pd.read_csv(Path(input_folder) / 'Index_Values.csv')
    max_Date = index_val['IV_INDEX_DATE'].max()
    date_object = datetime.strptime(max_Date, "%Y-%m-%d")
    month = date_object.month
    day = date_object.day
    year = date_object.year
    iss_pol= policy_GMDB.loc[policy_GMDB['TRANSACTION_DESC'] == 'Unplanned Premium', 'POLICY_NUM']

    # remove dupes
    for pol in iss_pol:
        policy_GMDB = policy_GMDB.drop(policy_GMDB[policy_GMDB['POLICY_NUM'] == pol].index)
    policy_list = policy_GMDB['POLICY_NUM'].unique()

    csv_payload = {
        'dtMonth': month,
        'dtDay': day,
        'dtYear': year,
        }


    with requests.session() as s:
            s.post('https://live.fo.annuities.crbgf.net/login', cred_payload, verify = False)
            print('User-Login processed. {0:0.1f}'.format(te.time() - start))
            for item in policy_list:
                r = s.get(secure_url, verify = False)

                policy_payload = {
                    'headerAccountNumber' : item,
                    }
                r2 = s.post(account_into, data=policy_payload, verify = False)
                
                r3 = s.post(cvq, data = csv_payload, verify = False)
                soup = BeautifulSoup(r3.content, 'html.parser')

                f = open(Path(input_folder) / "log.txt", "w")
                print(r3.text, file = f)
                
                # cleanup hmtl output step 1   
                with open (Path(input_folder) / 'log.txt', 'r+') as fp:
                    content = fp.read()
                    content = content.replace('<td class="tabletextblack" nowrap> ', "")
                    content = content.replace("<!-- TCS Changes for Source scan vulnerability fix Ends dated 2021-07-02-->", "")
                    content = content.replace("<!-- TCS Changes for Source scan vulnerability fix Starts dated 2021-06-30-->", "")
                    content = content.replace(",", "")                   
                    content = content.replace(":", ": ")
                    
                    fp.seek(0)
                    fp.truncate(0)
                    for line in content:
                        if not line.isspace() and not line.startswith(csv_Str):
                            fp.write(line)
                                    
                with open(Path(input_folder) / 'log.txt', 'r') as source_file, open(Path(input_folder) / 'updated_log.txt', 'w') as destination_file:
                    for line in source_file:
                        destination_file.write(line)

                # cleanup hmtl output step 2
                with open (Path(input_folder) / 'updated_log.txt', 'r+') as up:
                    up_content = up.read()                    
                    up_content = up_content.replace(":", ":")
                    up_content = up_content.replace('<tdclass="tabletextblack"nowrap>',"")
                    up_content = up_content.replace ("<!--SCRDec-", "")
                    up_content = up_content.replace("<!--SCR33052START-->", "")
                    up_content = up_content.replace("->", ",")
                    up_content = up_content.replace("</", ", ")
                    up.seek(0)
                    up.truncate(0)
                    up.write(up_content)

                
                # formatting outputs
                df1 = pd.read_csv(Path(input_folder) / "updated_log.txt")
                result = [col for col in df1.columns if 'CashSurrenderValue' in col or 'MinimumWithdrawalValue' in col or 'AccumulatedValue:' in col or 'SurrenderPenalty' in col or 'TotalDeathBenefit:' in col]
                
                death_benefit = result[0].split(":",1)
                death_benefit = float(death_benefit[1])
                accumulated_value = result[1].split(":",1)
                accumulated_value = float(accumulated_value [1])
                csv_value = result[2].split(":",1)
                csv_value = float(csv_value [1])
                mwv_value = result[3].split(":",1)
                mwv_value  = float(mwv_value [1])
                Surr_charge = result[4].split(":",1)
                Surr_charge = float(Surr_charge[1])
                
                
                data ={
                       "Processed Date":max_Date, 
                       "Policy Number": item, 
                       "CashSurrenderValue": csv_value,
                       "MinimumWithdrawalValue": mwv_value,
                       "AccumulatedValue:": accumulated_value, 
                       "SurrenderPenalty": Surr_charge, 
                       "TotalDeathBenefit:": death_benefit
                       }
                df_new_rows = pd.DataFrame(data, index=[0])
                if not df_new_rows.empty:
                    policy_data = pd.concat([i.dropna(axis = 1, how = 'all') for i in [df_new_rows, policy_data]])
                
                count = count + 1
                print(count)
                print(item)
    policy_data.to_csv(Path(input_folder) / 'FO_WebScrapped_Data.csv', index = False)

    s.close()
    _logger.info(f"Web scrapper finished...")



if __name__ == "__main__":
    # retrieve_data_from_Navisys('2026-03-24', 'C:\\AAA_RILA_Validation_Modernization\\RILA_Python\\Input_RILA\\20260324')
    # FO_Scrapper()
    pass