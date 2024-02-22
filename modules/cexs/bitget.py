import asyncio
import hmac
import json
import time

from hashlib import sha256

from general_settings import BITGET_API_PASSPHRAS
from modules import CEX, Logger
from modules.interfaces import SoftwareExceptionWithoutRetry
from utils.tools import helper
from config import CEX_WRAPPED_ID, TOKENS_PER_CHAIN, BITGET_NETWORKS_NAME


class Bitget(CEX, Logger):
    def __init__(self, client):
        self.client = client
        Logger.__init__(self)
        CEX.__init__(self, client, 'BingX')

        self.api_url = "https://api.bitget.com"
        self.headers = {
            "Content-Type": "application/json",
            "X-BX-APIKEY": self.api_key,
        }

    @staticmethod
    def parse_params(params: dict | None = None):
        if params:
            sorted_keys = sorted(params)
            params_str = f'?{"&".join(["%s=%s" % (x, params[x]) for x in sorted_keys])}'
        else:
            params_str = ''
        return params_str

    def get_headers(self, method:str, api_path:str, params:dict = None, payload:dict | str = None):
        try:
            timestamp = int(time.time() * 1000)
            if method == 'GET':
                api_path = f"{api_path}{self.parse_params(params)}"
            message = f"{timestamp}{method}{api_path}{json.dumps(payload) if payload else ''}"

            secret_key_bytes = self.api_secret.encode('utf-8')
            signature = hmac.new(secret_key_bytes, message.encode('utf-8'), sha256).hexdigest()

            return {
                "ACCESS-KEY": self.api_key,
                "ACCESS-SIGN": signature,
                "ACCESS-PASSPHRASE": BITGET_API_PASSPHRAS,
                "ACCESS-TIMESTAMP": f"{int(time.time() * 1000)}",
                "locale": "en-US",
                "Content-Type": "application/json"
            }
        except Exception as error:
            raise SoftwareExceptionWithoutRetry(f'Bad headers for BitGet request: {error}')

    async def get_balance(self, ccy: str):
        path = '/api/v2/spot/account/assets'

        params = {
            'coin': ccy
        }

        url = f"{self.api_url}{path}"
        data = await self.make_request(url=url, params=params, module_name='Balances Data')
        return data[0]['available']

    async def get_currencies(self, ccy):
        path = '/api/v2/spot/public/coins'

        params = {
            'coin': ccy
        }

        url = f"{self.api_url}{path}"
        return await self.make_request(url=url, params=params, module_name='Token info')

    @helper
    async def withdraw(self, withdraw_data:tuple = None):
        path = '/api/v2/spot/wallet/withdrawal'

        network_id, amount = withdraw_data
        network_raw_name = BITGET_NETWORKS_NAME[network_id]
        ccy, network_name = network_raw_name.split('-')
        dst_chain_id = CEX_WRAPPED_ID[network_id]
        withdraw_data = (await self.get_currencies(ccy))[0]['chains']

        network_data = {
            item['chain']: {
                'withdrawEnable': item['withdrawable'],
                'withdrawFee': item['withdrawFee'],
                'withdrawMin': item['minWithdrawAmount'],
            } for item in withdraw_data
        }[network_name]

        amount = await self.client.get_smart_amount(amount)

        self.logger_msg(*self.client.acc_info, msg=f"Withdraw {amount:.5f} {ccy} to {network_name}")

        if network_data['withdrawEnable']:
            min_wd = float(network_data['withdrawMin'])

            if min_wd <= amount:

                payload = {
                    "address": self.client.address,
                    "size": amount,
                    "coin": ccy,
                    "transferType": 'on_chain',
                    "chain": network_name,
                }

                ccy = f"{ccy}.e" if network_id in [29, 30] else ccy

                old_balance_on_dst = await self.client.wait_for_receiving(dst_chain_id, token_name=ccy,
                                                                          check_balance_on_dst=True)

                url = f"{self.api_url}{path}"
                headers = self.get_headers('POST', path, payload=payload)
                await self.make_request(method='POST', url=url, headers=headers, module_name='Withdraw')

                self.logger_msg(*self.client.acc_info,
                                msg=f"Withdraw complete. Note: wait a little for receiving funds", type_msg='success')

                await self.client.wait_for_receiving(dst_chain_id, old_balance_on_dst, token_name=ccy)

                return True
            else:
                raise SoftwareExceptionWithoutRetry(f"Limit range for withdraw: more than {min_wd:.5f} {ccy}")
        else:
            raise SoftwareExceptionWithoutRetry(f"Withdraw from {network_name} is not available")

    async def get_sub_balances(self):
        path = "/api/v2/spot/account/subaccount-assets"

        url = f"{self.api_url}{path}"
        headers = self.get_headers('GET', path)
        await asyncio.sleep(2)
        return await self.make_request(url=url, headers=headers, module_name='Get subAccounts balances')

    async def get_main_info(self):
        path = '/api/v2/spot/account/info'

        url = f"{self.api_url}{path}"
        headers = self.get_headers('GET', path)
        await asyncio.sleep(2)
        return await self.make_request(url=url, headers=headers, module_name='Get main account info')

    async def get_main_balance(self, ccy):
        path = '/api/v2/spot/account/assets'

        params = {
            'coin': ccy
        }

        url = f"{self.api_url}{path}"
        headers = self.get_headers('GET', path, params=params)
        data = await self.make_request(url=url, params=params, headers=headers, module_name='Main account balance')
        return data[0]['available']

    async def transfer_from_subaccounts(self, ccy: str = 'ETH', amount: float = None):

        if ccy == 'USDC.e':
            ccy = 'USDC'

        self.logger_msg(*self.client.acc_info, msg=f'Checking subAccounts balance')

        flag = True
        sub_list = await self.get_sub_balances()
        main_id = (await self.get_main_info())['userId']

        for sub_data in sub_list:
            sub_id = sub_data['userId']
            sub_balances = sub_data['assetsList']
            sub_balance = float([balance for balance in sub_balances if balance['coin'] == ccy][0]['available'])

            if sub_balance != 0.0:
                flag = False
                self.logger_msg(*self.client.acc_info, msg=f'{sub_id} | subAccount balance : {sub_balance} {ccy}')

                payload = {
                    "amount": amount,
                    "coin": ccy,
                    "fromType": "spot",
                    "toType": "spot",
                    "fromUserId": sub_id,
                    "toUserId": main_id,
                }

                path = "/api/v2/spot/wallet/subaccount-transfer"
                url = f"{self.api_url}{path}"
                headers = self.get_headers('POST', path, payload=payload)
                await self.make_request(method="POST", url=url, headers=headers, module_name='SubAccount transfer')

                self.logger_msg(*self.client.acc_info,
                                msg=f"Transfer {amount} {ccy} to main account complete", type_msg='success')
        if flag:
            self.logger_msg(*self.client.acc_info, msg=f'subAccounts balance: 0 {ccy}', type_msg='warning')
        return True

    async def get_cex_balances(self, ccy: str = 'ETH'):

        if ccy == 'USDC.e':
            ccy = 'USDC'

        balances = {}

        main_balances = await self.get_main_balance(ccy)

        available_balance = [balance for balance in main_balances if balance['coin'] == ccy]

        if available_balance:
            balances['Main CEX Account'] = float(available_balance[0]['available'])

        sub_list = await self.get_sub_balances()

        for sub_data in sub_list:
            sub_name = sub_data['userId']
            sub_balances = sub_data['assetsList']
            balances[sub_name] = float([balance for balance in sub_balances if balance['coin'] == ccy][0]['available'])

            await asyncio.sleep(3)

        return balances

    async def wait_deposit_confirmation(self, amount: float, old_balances: dict, ccy: str = 'ETH',
                                        check_time: int = 45, timeout: int = 1200):

        if ccy == 'USDC.e':
            ccy = 'USDC'

        self.logger_msg(*self.client.acc_info, msg=f"Start checking CEX balances")

        await asyncio.sleep(10)
        total_time = 0
        while total_time < timeout:
            new_sub_balances = await self.get_cex_balances(ccy=ccy)
            for acc_name, acc_balance in new_sub_balances.items():

                if acc_balance > old_balances[acc_name]:
                    self.logger_msg(*self.client.acc_info, msg=f"Deposit {amount} {ccy} complete", type_msg='success')
                    return True
                else:
                    continue
            else:
                total_time += check_time
                self.logger_msg(*self.client.acc_info, msg=f"Deposit still in progress...", type_msg='warning')
                await asyncio.sleep(check_time)

        self.logger_msg(*self.client.acc_info, msg=f"Deposit does not complete in {timeout} seconds", type_msg='error')

    @helper
    async def deposit(self, deposit_data:tuple = None):
        try:
            with open('./data/services/cex_withdraw_list.json') as file:
                from json import load
                cex_withdraw_list = load(file)
        except:
            self.logger_msg(None, None, f"Bad data in cex_wallet_list.json", 'error')

        try:
            cex_wallet = cex_withdraw_list[self.client.account_name]
        except Exception as error:
            raise SoftwareExceptionWithoutRetry(f'There is no wallet listed for deposit to CEX: {error}')

        info = f"{cex_wallet[:10]}....{cex_wallet[-6:]}"

        deposit_network, deposit_amount = deposit_data
        network_raw_name = BITGET_NETWORKS_NAME[deposit_network]
        ccy, network_name = network_raw_name.split('-')
        withdraw_data = (await self.get_currencies(ccy))[0]['chains']

        network_data = {
            item['chain']: {
                'depositEnable': item['rechargeable']
            } for item in withdraw_data
        }[network_name]

        ccy = f"{ccy}.e" if deposit_network in [29, 30] else ccy
        amount = await self.client.get_smart_amount(deposit_amount, token_name=ccy)

        self.logger_msg(*self.client.acc_info, msg=f"Deposit {amount} {ccy} from {network_name} to BingX wallet: {info}")

        if network_data['depositEnable']:

            if ccy != self.client.token:
                token_contract = self.client.get_contract(TOKENS_PER_CHAIN[self.client.network.name][ccy])
                decimals = await self.client.get_decimals(ccy)
                amount_in_wei = self.client.to_wei(amount, decimals)

                transaction = await token_contract.functions.transfer(
                    self.client.w3.to_checksum_address(cex_wallet),
                    amount_in_wei
                ).build_transaction(await self.client.prepare_transaction())
            else:
                amount_in_wei = self.client.to_wei(amount)
                transaction = (await self.client.prepare_transaction(value=int(amount_in_wei))) | {
                    'to': self.client.w3.to_checksum_address(cex_wallet),
                    'data': '0x'
                }

            cex_balances = await self.get_cex_balances(ccy=ccy)

            result = await self.client.send_transaction(transaction)

            await self.wait_deposit_confirmation(amount, cex_balances, ccy=ccy)

            await self.transfer_from_subaccounts(ccy=ccy, amount=amount)

            return result
        else:
            raise SoftwareExceptionWithoutRetry(f"Deposit to {network_name} is not available")