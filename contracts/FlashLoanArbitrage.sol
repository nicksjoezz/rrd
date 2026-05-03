// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title FlashLoanArbitrage
 * @dev Flexible Arbitrage Executor for Multi-hop paths.
 * Supports Uniswap V2, Uniswap V3, Camelot V2, and Camelot V3 (Algebra).
 */

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
}

interface IUniswapV3Pool {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function flash(
        address recipient,
        uint256 amount0,
        uint256 amount1,
        bytes calldata data
    ) external;
}

interface IUniswapV2Router {
    function swapExactTokensForTokensSupportingFeeOnTransferTokens(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        uint deadline
    ) external;
}

interface ICamelotRouter {
    function swapExactTokensForTokensSupportingFeeOnTransferTokens(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        address referrer,
        uint deadline
    ) external;
}

interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external returns (uint256 amountOut);
}

interface ICamelotV3Router {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 limitSqrtPrice;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external returns (uint256 amountOut);
}

contract FlashLoanArbitrage {
    address public owner;

    // Arbitrum Mainnet Addresses
    address public constant UNIV3_ROUTER      = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address public constant UNIV2_ROUTER      = 0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24;
    address public constant CAMELOT_V2_ROUTER = 0xc873fEcbd354f5A56E00E710B90EF4201db2448d;
    address public constant CAMELOT_V3_ROUTER = 0x1F721E2E82F6676FCE4eA07A5958cF098D339e18;

    enum DexType { UNIV2, UNIV3, CAMELOTV2, CAMELOTV3 }

    struct SwapStep {
        DexType dex;
        address tokenIn;
        address tokenOut;
        uint24 fee; // for UniV3
    }

    event ArbExecuted(uint256 amountIn, uint256 profit);

    constructor() {
        owner = msg.sender;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    function executeArb(
        address flashPool,
        uint256 amountFlash,
        address tokenFlash,
        SwapStep[] calldata steps,
        uint256 minProfit
    ) external onlyOwner {
        bool isToken0 = IUniswapV3Pool(flashPool).token0() == tokenFlash;
        uint256 amount0 = isToken0 ? amountFlash : 0;
        uint256 amount1 = isToken0 ? 0 : amountFlash;

        IUniswapV3Pool(flashPool).flash(
            address(this),
            amount0,
            amount1,
            abi.encode(flashPool, tokenFlash, amountFlash, steps, minProfit)
        );
    }

    function uniswapV3FlashCallback(
        uint256 fee0,
        uint256 fee1,
        bytes calldata data
    ) external {
        (address flashPool, address tokenFlash, uint256 amountFlash, SwapStep[] memory steps, uint256 minProfit) =
            abi.decode(data, (address, address, uint256, SwapStep[], uint256));

        require(msg.sender == flashPool, "Unauthorized callback");

        uint256 fee = fee0 > 0 ? fee0 : fee1;
        uint256 amountToRepay = amountFlash + fee;
        uint256 balBefore = IERC20(tokenFlash).balanceOf(address(this));

        for (uint256 i = 0; i < steps.length; i++) {
            SwapStep memory step = steps[i];
            uint256 amountIn = IERC20(step.tokenIn).balanceOf(address(this));

            if (step.dex == DexType.UNIV3) {
                IERC20(step.tokenIn).approve(UNIV3_ROUTER, amountIn);
                ISwapRouter(UNIV3_ROUTER).exactInputSingle(
                    ISwapRouter.ExactInputSingleParams({
                        tokenIn: step.tokenIn,
                        tokenOut: step.tokenOut,
                        fee: step.fee,
                        recipient: address(this),
                        deadline: block.timestamp,
                        amountIn: amountIn,
                        amountOutMinimum: 0,
                        sqrtPriceLimitX96: 0
                    })
                );
            } else if (step.dex == DexType.CAMELOTV3) {
                IERC20(step.tokenIn).approve(CAMELOT_V3_ROUTER, amountIn);
                ICamelotV3Router(CAMELOT_V3_ROUTER).exactInputSingle(
                    ICamelotV3Router.ExactInputSingleParams({
                        tokenIn: step.tokenIn,
                        tokenOut: step.tokenOut,
                        recipient: address(this),
                        deadline: block.timestamp,
                        amountIn: amountIn,
                        amountOutMinimum: 0,
                        limitSqrtPrice: 0
                    })
                );
            } else if (step.dex == DexType.CAMELOTV2) {
                IERC20(step.tokenIn).approve(CAMELOT_V2_ROUTER, amountIn);
                address[] memory path = new address[](2);
                path[0] = step.tokenIn;
                path[1] = step.tokenOut;
                ICamelotRouter(CAMELOT_V2_ROUTER).swapExactTokensForTokensSupportingFeeOnTransferTokens(
                    amountIn,
                    0,
                    path,
                    address(this),
                    address(0),
                    block.timestamp
                );
            } else if (step.dex == DexType.UNIV2) {
                IERC20(step.tokenIn).approve(UNIV2_ROUTER, amountIn);
                address[] memory path = new address[](2);
                path[0] = step.tokenIn;
                path[1] = step.tokenOut;
                IUniswapV2Router(UNIV2_ROUTER).swapExactTokensForTokensSupportingFeeOnTransferTokens(
                    amountIn,
                    0,
                    path,
                    address(this),
                    block.timestamp
                );
            }
        }

        // Repay
        IERC20(tokenFlash).transfer(msg.sender, amountToRepay);

        uint256 balAfter = IERC20(tokenFlash).balanceOf(address(this));
        require(balAfter >= balBefore + minProfit, "Insufficient profit");

        emit ArbExecuted(amountFlash, balAfter - balBefore);

        // Withdraw profit
        if (balAfter > 0) {
            IERC20(tokenFlash).transfer(owner, balAfter);
        }
    }

    function withdraw(address token) external onlyOwner {
        uint256 bal = IERC20(token).balanceOf(address(this));
        IERC20(token).transfer(owner, bal);
    }

    function withdrawETH() external onlyOwner {
        payable(owner).transfer(address(this).balance);
    }

    receive() external payable {}
}
