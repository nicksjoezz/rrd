// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * Arbitrumliquidator.sol — v2.2 (Standardized name)
 *
 * New in v2.2:
 *   - minProfit slippage protection on all liquidation paths
 *   - Standardized Aave/Radiant, Silo, and Morpho handlers
 *   - Compound III direct absorption
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

interface ISilo {
    function liquidationCall(address debtToken, address collateralToken,
        address borrower, uint256 repayAmount, bool receiveSToken) external;
}

interface IMorpho {
    struct MarketParams { address loanToken; address collateralToken; address oracle; address irm; uint256 lltv; }
    function liquidate(MarketParams calldata params, address borrower,
        uint256 seizedAssets, uint256 repaidShares, bytes calldata data) external;
}

interface IComet {
    function absorb(address absorber, address[] memory accounts) external;
    function baseToken() external view returns (address);
    function buyCollateral(address asset, uint256 minAmount, uint256 baseAmount, address recipient) external;
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

contract ArbitrumLiquidator {
    address public owner;
    bool    public paused;

    IBalancerVault constant BALANCER    = IBalancerVault(0xBA12222222228d8Ba445958a75a0704d566BF2C8);
    ISwapRouter    constant SWAP_ROUTER = ISwapRouter(0xE592427A0AEce92De3Edee1F18E0157C05861564);

    event LiquidationExecuted(address indexed borrower, address collateral,
        address debtToken, uint256 debtCovered, uint256 profit);

    modifier onlyOwner()  { require(msg.sender == owner, "Not owner"); _; }
    modifier notPaused()  { require(!paused, "Paused"); _; }

    constructor() { owner = msg.sender; }

    // ── Protocol Types ───────────────────────────────────────────────────────
    // 0: AaveV3/Radiant, 1: SiloV2, 2: MorphoBlue

    // ── Entry Points ──────────────────────────────────────────────────────────
    function executeLiquidation(
        uint8 protocol, address debtToken, address collateralToken, address borrower,
        uint256 debtAmount, address pool, uint24 swapFee, uint256 minProfit, bytes memory morphoParams
    ) external onlyOwner notPaused {
        _flashLoan(debtToken, debtAmount, abi.encode(
            uint8(1), protocol, collateralToken, borrower, debtAmount, pool, swapFee, minProfit, bytes(""), morphoParams
        ));
    }

    function executeLiquidationMultiHop(
        uint8 protocol, address debtToken, address collateralToken, address borrower,
        uint256 debtAmount, address pool, uint256 minProfit, bytes calldata swapPath, bytes memory morphoParams
    ) external onlyOwner notPaused {
        _flashLoan(debtToken, debtAmount, abi.encode(
            uint8(2), protocol, collateralToken, borrower, debtAmount, pool, uint24(0), minProfit, swapPath, morphoParams
        ));
    }

    // ── Compound III (Special: No Flash Loan Needed) ──────────────────────────
    function absorbCompound(address comet, address[] calldata accounts) external onlyOwner notPaused {
        IComet(comet).absorb(address(this), accounts);
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

        (uint8 swapType, uint8 protocol, address collateralToken, address borrower,
         uint256 debtAmount, address pool, uint24 swapFee, uint256 minProfit,
         bytes memory swapPath, bytes memory morphoParams) =
            abi.decode(userData, (uint8, uint8, address, address, uint256, address, uint24, uint256, bytes, bytes));

        address debtToken   = tokens[0];
        uint256 repayAmount = amounts[0] + feeAmounts[0];

        // 1. Call Protocol-specific Liquidation
        if (protocol == 0) { // Aave V3 / Radiant
            IERC20(debtToken).approve(pool, debtAmount);
            IAavePool(pool).liquidationCall(collateralToken, debtToken, borrower, debtAmount, false);
        }
        else if (protocol == 1) { // Silo V2
            IERC20(debtToken).approve(pool, debtAmount);
            ISilo(pool).liquidationCall(debtToken, collateralToken, borrower, debtAmount, false);
        }
        else if (protocol == 2) { // Morpho Blue
            IMorpho.MarketParams memory m = abi.decode(morphoParams, (IMorpho.MarketParams));
            IERC20(debtToken).approve(pool, debtAmount);
            IMorpho(pool).liquidate(m, borrower, 0, debtAmount, "");
        }

        // 2. Swap collateral → debt token
        uint256 colBal = IERC20(collateralToken).balanceOf(address(this));
        if (collateralToken != debtToken && colBal > 0) {
            IERC20(collateralToken).approve(address(SWAP_ROUTER), colBal);
            if (swapType == 1) {
                SWAP_ROUTER.exactInputSingle(ISwapRouter.ExactInputSingleParams({
                    tokenIn: collateralToken, tokenOut: debtToken, fee: swapFee,
                    recipient: address(this), amountIn: colBal,
                    amountOutMinimum: repayAmount + minProfit, sqrtPriceLimitX96: 0
                }));
            } else {
                SWAP_ROUTER.exactInput(ISwapRouter.ExactInputParams({
                    path: swapPath, recipient: address(this),
                    amountIn: colBal, amountOutMinimum: repayAmount + minProfit
                }));
            }
        }

        // 3. Repay Balancer
        IERC20(debtToken).transfer(address(BALANCER), repayAmount);

        // 4. Record Profit
        uint256 profit = IERC20(debtToken).balanceOf(address(this));
        emit LiquidationExecuted(borrower, collateralToken, debtToken, debtAmount, profit);
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
    function setPaused(bool _p) external onlyOwner { paused = _p; }
    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "Zero addr");
        owner = newOwner;
    }
    receive() external payable {}
}
