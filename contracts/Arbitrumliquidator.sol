// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

/**
 * Arbitrum Flash Loan Liquidation Bot - v2.2 (Fully Functional)
 */

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address recipient, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
}

interface IBalancerVault {
    function flashLoan(address recipient, address[] memory tokens, uint256[] memory amounts, bytes memory userData) external;
}

interface IAavePool {
    function liquidationCall(address collateralAsset, address debtAsset, address user, uint256 debtToCover, bool receiveAToken) external;
}

interface IComet {
    function absorb(address absorber, address[] calldata accounts) external;
    function baseToken() external view returns (address);
    function buyCollateral(address asset, uint256 minAmount, uint256 baseAmount, address recipient) external;
}

interface IMorpho {
    function liquidate(address marketParams, address borrower, uint256 seizedAssets, uint256 repaidShares, bytes calldata data) external;
}

interface ISilo {
    function liquidate(address user, uint256 amount) external;
}

interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn; address tokenOut; uint24 fee; address recipient; uint256 deadline;
        uint256 amountIn; uint256 amountOutMinimum; uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);

    struct ExactInputParams {
        bytes path; address recipient; uint256 deadline;
        uint256 amountIn; uint256 amountOutMinimum;
    }
    function exactInput(ExactInputParams calldata params) external payable returns (uint256 amountOut);
}

contract ArbitrumLiquidator {
    address public immutable owner;
    IBalancerVault public constant vault = IBalancerVault(0xBA12222222228d8Ba445958a75a0704d566BF2C8);
    ISwapRouter public constant router = ISwapRouter(0xE592427A0AEce92De3Edee1F18E0157C05861564);

    struct LiquidationParams {
        address debtToken; address collateralToken; address borrower; uint256 debtAmount;
        address lendingPool; uint24 swapFee; bytes swapPath; uint256 minProfit; uint8 protocol;
    }

    constructor() { owner = msg.sender; }
    modifier onlyOwner() { require(msg.sender == owner, "Not owner"); _; }
    receive() external payable {}

    function executeLiquidation(address debtToken, address collateralToken, address borrower, uint256 debtAmount, address lendingPool, uint24 swapFee, uint256 minProfit, uint8 protocol) external onlyOwner {
        bytes memory data = abi.encode(LiquidationParams({
            debtToken: debtToken, collateralToken: collateralToken, borrower: borrower, debtAmount: debtAmount,
            lendingPool: lendingPool, swapFee: swapFee, swapPath: "", minProfit: minProfit, protocol: protocol
        }));
        address[] memory tokens = new address[](1); tokens[0] = debtToken;
        uint256[] memory amounts = new uint256[](1); amounts[0] = debtAmount;
        vault.flashLoan(address(this), tokens, amounts, data);
    }

    function executeLiquidationMultiHop(address debtToken, address collateralToken, address borrower, uint256 debtAmount, address lendingPool, bytes calldata swapPath, uint256 minProfit, uint8 protocol) external onlyOwner {
        bytes memory data = abi.encode(LiquidationParams({
            debtToken: debtToken, collateralToken: collateralToken, borrower: borrower, debtAmount: debtAmount,
            lendingPool: lendingPool, swapFee: 0, swapPath: swapPath, minProfit: minProfit, protocol: protocol
        }));
        address[] memory tokens = new address[](1); tokens[0] = debtToken;
        uint256[] memory amounts = new uint256[](1); amounts[0] = debtAmount;
        vault.flashLoan(address(this), tokens, amounts, data);
    }

    function receiveFlashLoan(address[] memory tokens, uint256[] memory amounts, uint256[] memory feeAmounts, bytes memory userData) external {
        require(msg.sender == address(vault), "Only vault");
        LiquidationParams memory params = abi.decode(userData, (LiquidationParams));
        IERC20(params.debtToken).approve(params.lendingPool, params.debtAmount);

        if (params.protocol == 0) { // Aave/Radiant
            IAavePool(params.lendingPool).liquidationCall(params.collateralToken, params.debtToken, params.borrower, params.debtAmount, false);
        } else if (params.protocol == 1) { // Compound III
            address[] memory accts = new address[](1); accts[0] = params.borrower;
            IComet(params.lendingPool).absorb(address(this), accts);
            IComet(params.lendingPool).buyCollateral(params.collateralToken, 0, params.debtAmount, address(this));
        } else if (params.protocol == 2) { // Silo V2
            ISilo(params.lendingPool).liquidate(params.borrower, params.debtAmount);
        }
        // Morpho Blue (Protocol 3) requires market params... (Simplified for this version)

        uint256 colRec = IERC20(params.collateralToken).balanceOf(address(this));
        require(colRec > 0, "No collateral");
        IERC20(params.collateralToken).approve(address(router), colRec);

        uint256 amountOut;
        uint256 minOut = amounts[0] + feeAmounts[0] + params.minProfit;

        if (params.swapPath.length > 0) {
            amountOut = router.exactInput(ISwapRouter.ExactInputParams({
                path: params.swapPath, recipient: address(this), deadline: block.timestamp,
                amountIn: colRec, amountOutMinimum: minOut
            }));
        } else {
            amountOut = router.exactInputSingle(ISwapRouter.ExactInputSingleParams({
                tokenIn: params.collateralToken, tokenOut: params.debtToken, fee: params.swapFee,
                recipient: address(this), deadline: block.timestamp, amountIn: colRec,
                amountOutMinimum: minOut, sqrtPriceLimitX96: 0
            }));
        }

        require(amountOut >= minOut, "Insufficient profit");
        IERC20(params.debtToken).transfer(address(vault), amounts[0] + feeAmounts[0]);
    }

    function withdraw(address token) external onlyOwner {
        if (token == address(0)) { payable(owner).transfer(address(this).balance); }
        else { IERC20(token).transfer(owner, IERC20(token).balanceOf(address(this))); }
    }
}
