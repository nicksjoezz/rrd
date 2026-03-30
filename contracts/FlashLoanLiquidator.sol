// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * FlashLoanLiquidator.sol — v2
 *
 * New in v2:
 *   - Multi-hop swap (GMX → WETH → USDC) for better price execution
 *   - Emergency pause via owner
 *   - Supports both single-hop and multi-hop liquidations
 *
 * Deploy: Remix IDE → Solidity 0.8.19 → Arbitrum mainnet
 */

interface IBalancerVault {
    function flashLoan(address recipient, address[] memory tokens,
        uint256[] memory amounts, bytes memory userData) external;
}

interface IAavePool {
    function liquidationCall(address collateralAsset, address debtAsset,
        address user, uint256 debtToCover, bool receiveAToken) external;
}

interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn; address tokenOut; uint24 fee; address recipient;
        uint256 amountIn; uint256 amountOutMinimum; uint160 sqrtPriceLimitX96;
    }
    struct ExactInputParams {
        bytes path; address recipient; uint256 amountIn; uint256 amountOutMinimum;
    }
    function exactInputSingle(ExactInputSingleParams calldata p) external returns (uint256);
    function exactInput(ExactInputParams calldata p) external returns (uint256);
}

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

contract FlashLoanLiquidator {
    address public owner;
    bool    public paused;

    IBalancerVault constant BALANCER    = IBalancerVault(0xBA12222222228d8Ba445958a75a0704d566BF2C8);
    ISwapRouter    constant SWAP_ROUTER = ISwapRouter(0xE592427A0AEce92De3Edee1F18E0157C05861564);

    event LiquidationExecuted(address indexed borrower, address collateral,
        address debtToken, uint256 debtCovered, uint256 profit);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event Paused(address account);
    event Unpaused(address account);

    modifier onlyOwner()  { require(msg.sender == owner, "Not owner"); _; }
    modifier notPaused()  { require(!paused, "Paused"); _; }

    constructor() {
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    // ── Single-hop (ETH/BTC/stablecoin collateral) ────────────────────────────
    function executeLiquidation(
        address debtToken, address collateralToken, address borrower,
        uint256 debtAmount, address lendingPool, uint24 swapFee, uint256 minProfit
    ) external onlyOwner notPaused {
        _flashLoan(debtToken, debtAmount, abi.encode(
            uint8(1), collateralToken, borrower, debtAmount, lendingPool, swapFee, bytes(""), minProfit
        ));
    }

    // ── Multi-hop (ARB/GMX/LINK collateral for better pricing) ────────────────
    function executeLiquidationMultiHop(
        address debtToken, address collateralToken, address borrower,
        uint256 debtAmount, address lendingPool, bytes calldata swapPath, uint256 minProfit
    ) external onlyOwner notPaused {
        _flashLoan(debtToken, debtAmount, abi.encode(
            uint8(2), collateralToken, borrower, debtAmount, lendingPool, uint24(0), swapPath, minProfit
        ));
    }

    function _flashLoan(address token, uint256 amount, bytes memory userData) internal {
        address[] memory tokens  = new address[](1);
        uint256[] memory amounts = new uint256[](1);
        tokens[0] = token; amounts[0] = amount;
        BALANCER.flashLoan(address(this), tokens, amounts, userData);
    }

    // ── Balancer callback ──────────────────────────────────────────────────────
    function receiveFlashLoan(
        address[] memory tokens, uint256[] memory amounts,
        uint256[] memory feeAmounts, bytes memory userData
    ) external {
        require(msg.sender == address(BALANCER), "Unauthorized");

        (uint8 swapType, address collateralToken, address borrower,
         uint256 debtAmount, address lendingPool, uint24 swapFee,
         bytes memory swapPath, uint256 minProfit) =
            abi.decode(userData, (uint8, address, address, uint256, address, uint24, bytes, uint256));

        address debtToken   = tokens[0];
        uint256 repayAmount = amounts[0] + feeAmounts[0]; // feeAmounts[0] == 0 on Balancer

        // 1. Approve Aave pool
        IERC20(debtToken).approve(lendingPool, debtAmount);

        // 2. Liquidate — receive collateral + bonus
        IAavePool(lendingPool).liquidationCall(
            collateralToken, debtToken, borrower, debtAmount, false
        );

        // 3. Swap collateral → debt token
        uint256 colBal = IERC20(collateralToken).balanceOf(address(this));
        if (collateralToken != debtToken && colBal > 0) {
            IERC20(collateralToken).approve(address(SWAP_ROUTER), colBal);
            uint256 minAmountOut = repayAmount + minProfit;
            if (swapType == 1) {
                SWAP_ROUTER.exactInputSingle(ISwapRouter.ExactInputSingleParams({
                    tokenIn: collateralToken, tokenOut: debtToken, fee: swapFee,
                    recipient: address(this), amountIn: colBal,
                    amountOutMinimum: minAmountOut, sqrtPriceLimitX96: 0
                }));
            } else {
                SWAP_ROUTER.exactInput(ISwapRouter.ExactInputParams({
                    path: swapPath, recipient: address(this),
                    amountIn: colBal, amountOutMinimum: minAmountOut
                }));
            }
        }

        // 4. Repay Balancer (fee = 0)
        IERC20(debtToken).transfer(address(BALANCER), repayAmount);

        // 5. Profit stays — owner calls withdraw()
        uint256 balanceAfter = IERC20(debtToken).balanceOf(address(this));
        // Safety check to ensure we didn't lose funds (though Balancer check would fail anyway)
        require(balanceAfter >= minProfit, "minProfit not met");

        emit LiquidationExecuted(borrower, collateralToken, debtToken, debtAmount, balanceAfter);
    }

    // ── Admin ─────────────────────────────────────────────────────────────────
    function withdraw(address token) external onlyOwner {
        uint256 bal = IERC20(token).balanceOf(address(this));
        require(bal > 0, "Nothing to withdraw");
        IERC20(token).transfer(owner, bal);
    }
    function withdrawETH() external onlyOwner {
        payable(owner).transfer(address(this).balance);
    }
    function setPaused(bool _p) external onlyOwner {
        paused = _p;
        if (_p) emit Paused(msg.sender);
        else emit Unpaused(msg.sender);
    }
    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "Zero addr");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }
    receive() external payable {}
}
